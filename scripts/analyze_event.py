#!/usr/bin/env python3
"""Per-event LLM scoring: event-specific phrase probability adjustments.

Unlike the global analyze_signals.py (which produces phrase boosts based on the
general news cycle), this script produces PER-EVENT multipliers that reflect the
specific format, participants, and topic of each live or scheduled Kalshi event.

Key insight: A phrase like "baseball" might have a 12% historical base rate for
Trump. But at a formal diplomatic dinner with Japan's PM, that probability should
be suppressed to ~2-3%. Conversely, "toyota" (normally ~8%) should be boosted
because US-Japan automotive tariffs are a central topic.

Architecture:
  1. Load all live/scheduled events from the DB.
  2. For each event, classify its format (diplomatic, rally, presser, etc.).
  3. Build a rich event-specific context: title, format, participants, relevant topics.
  4. Call GPT-4o to assess per-phrase multipliers SPECIFIC to this event.
  5. Write results to data/event_signals/{safe_event_id}.json.

The scorer reads these files at runtime via EventSignalStore and applies them
as an additional multiplier: p = base * topic_rel * decay * news * buzz * global_llm * event_llm.

Run:
  python scripts/analyze_event.py                    # all live/scheduled events
  python scripts/analyze_event.py --event-id <id>    # single event
  python scripts/analyze_event.py --force            # skip freshness check
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
ROOT             = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.db import connect as _db_connect  # noqa: E402
DB_PATH          = ROOT / "data" / "edge.db"
SIGNAL_CTX_PATH  = ROOT / "data" / "signal_context.json"
OUTCOMES_PATH    = ROOT / "data" / "kalshi_outcomes.json"
EVENT_SIGNALS_DIR = ROOT / "data" / "event_signals"
ENV_PATH         = ROOT / "config" / "runtime.env"

LLM_MODEL         = os.getenv("LLM_EVENT_MODEL", "gpt-4o-mini")
REFRESH_SEC       = 21600  # re-analyze events every 6 hours (was 30 min)

# Series prefixes where LLM adds no value — phrase occurrence is driven by game
# mechanics / deterministic scripts, not political/news context.
_SPORTS_SERIES_PREFIXES = frozenset([
    "kxnbamention", "kxncaabmention", "kxmlbmention", "kxfightmention",
    "kxmrbeastmention", "kxsnlmention",
])

def _llm_extra_kwargs(model: str) -> dict:
    """gpt-5 / o-series models reject temperature != 1; omit it for those."""
    if model.startswith("gpt-5") or model.startswith("o"):
        return {}
    return {"temperature": 0.1}

def _max_tokens(base: int, model: str) -> int:
    """gpt-5 reasoning models burn hidden think tokens before output; 5x headroom needed."""
    if model.startswith("gpt-5") or model.startswith("o"):
        return base * 5
    return base
MAX_PHRASES_BATCH = 60   # phrases per LLM call (was 80; smaller = cheaper + faster)
MAX_PHRASES_TOTAL = 120  # cap per event — pick the most uncertain phrases only


# ── Config ────────────────────────────────────────────────────────────────────

def _load_env() -> None:
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _get_api_key() -> str:
    _load_env()
    key = os.getenv("OPENAI_API_KEY", "")
    if not key:
        logger.error("OPENAI_API_KEY not set — add to config/runtime.env")
        sys.exit(1)
    return key


def _client(api_key: str):
    from openai import OpenAI
    return OpenAI(api_key=api_key)


# ── DB helpers ────────────────────────────────────────────────────────────────

def _load_live_events(event_id: str | None = None) -> list[dict]:
    """Return live/scheduled events from DB. Optional filter by event_id."""
    if not DB_PATH.exists():
        return []
    conn = _db_connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if event_id:
            rows = conn.execute(
                "SELECT event_id, speaker, event_type, speech_state, notes, "
                "scheduled_start_ts FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT event_id, speaker, event_type, speech_state, notes, "
                "scheduled_start_ts FROM events "
                "WHERE speech_state IN ('live', 'scheduled') "
                "ORDER BY CASE speech_state WHEN 'live' THEN 0 ELSE 1 END, "
                "scheduled_start_ts ASC LIMIT 20",
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _load_phrases_for_event(event_id: str) -> list[str]:
    """Return all distinct phrases active for this event from action_cards."""
    if not DB_PATH.exists():
        return []
    parts = event_id.split(":", 2)
    event_ticker = parts[2] if len(parts) == 3 else event_id

    conn = _db_connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        # Get phrases from the latest action_card per market for this event
        rows = conn.execute(
            """
            SELECT DISTINCT json_extract(raw_json, '$.phrase') AS phrase
            FROM action_cards
            WHERE market_id LIKE ?
              AND json_extract(raw_json, '$.phrase') IS NOT NULL
              AND json_extract(raw_json, '$.phrase') != ''
              AND json_extract(raw_json, '$.phrase') != 'event does not qualify'
            """,
            (f"{event_ticker}%",),
        ).fetchall()
        return sorted({r["phrase"].lower().strip() for r in rows if r["phrase"]})
    finally:
        conn.close()


def _load_base_rates() -> dict[str, float]:
    """Return {phrase: yes_rate} from kalshi_outcomes.json."""
    if not OUTCOMES_PATH.exists():
        return {}
    try:
        data = json.loads(OUTCOMES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    counts: dict[str, list] = {}
    for m in data.get("markets", []):
        phrase = (m.get("primary_phrase") or "").lower().strip()
        result = m.get("result")
        if not phrase or result not in ("yes", "no"):
            continue
        if phrase not in counts:
            counts[phrase] = [0, 0]
        counts[phrase][1] += 1
        if result == "yes":
            counts[phrase][0] += 1
    return {
        p: round(yes / total, 3)
        for p, (yes, total) in counts.items()
        if total >= 3
    }


def _load_global_signal_context() -> str:
    """Return a compact summary from signal_context.json for LLM grounding."""
    if not SIGNAL_CTX_PATH.exists():
        return "(no global signal context available)"
    try:
        ctx = json.loads(SIGNAL_CTX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return "(could not load signal_context.json)"

    lines: list[str] = []
    fetched = ctx.get("fetched_at", "")[:16]
    lines.append(f"Global signal context as of {fetched} UTC:")

    wh = ctx.get("sources", {}).get("whitehouse_schedule", [])[:4]
    if wh:
        lines.append("WH schedule: " + " | ".join(e.get("title", "")[:60] for e in wh))

    ts = ctx.get("sources", {}).get("truth_social", [])[:3]
    if ts:
        lines.append("Truth Social recent posts:")
        for p in ts:
            txt = (p.get("content") or p.get("text") or "").strip()[:150]
            if txt:
                lines.append(f"  [{p.get('created_at','')[:10]}] {txt}")

    news = ctx.get("sources", {}).get("google_news", [])[:6]
    if news:
        lines.append("Headlines: " + " | ".join(h.get("title", "")[:60] for h in news))

    trends = ctx.get("sources", {}).get("google_trends", [])[:8]
    if trends:
        lines.append("Trends: " + ", ".join(
            f"{t['keyword']}({t['interest']})" for t in trends
        ))

    return "\n".join(lines)


# ── File helpers ──────────────────────────────────────────────────────────────

def _safe_filename(event_id: str) -> str:
    """Convert event_id to a safe filename."""
    return re.sub(r"[^\w\-]", "_", event_id) + ".json"


def _is_fresh(event_id: str) -> bool:
    """Return True if the event signal file exists and is less than REFRESH_SEC old."""
    path = EVENT_SIGNALS_DIR / _safe_filename(event_id)
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
        analyzed_at = data.get("analyzed_at", "")
        if not analyzed_at:
            return False
        dt = datetime.fromisoformat(analyzed_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(tz=timezone.utc) - dt).total_seconds()
        return age < REFRESH_SEC
    except Exception:
        return False


def _write_event_signals(event_id: str, data: dict) -> None:
    EVENT_SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    path = EVENT_SIGNALS_DIR / _safe_filename(event_id)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Wrote event signals → %s (%d adjustments)", path.name, len(data.get("adjustments", {})))


# ── Event format classification ───────────────────────────────────────────────

def _classify_event(event: dict, event_title: str = "") -> str:
    """Classify the event format using structured DB fields first, title text second.

    Priority order:
      1. event_type field (e.g. "press_briefing", "rally", "congressional_hearing")
      2. speaker field (leavitt/spicer/mcenany → briefing; trump → use title text)
      3. Title text pattern matching (last resort, skips per-phrase question text)
    """
    sys.path.insert(0, str(ROOT))
    from app.event_context import classify_event_format

    # --- 1. Structured event_type field wins first ---
    event_type = (event.get("event_type") or "").lower()
    _TYPE_MAP = {
        "press_briefing": "briefing",
        "wh_briefing": "briefing",
        "briefing": "briefing",
        "rally": "rally",
        "maga_rally": "rally",
        "presser": "presser",
        "press_conference": "presser",
        "testimony": "testimony",
        "hearing": "testimony",
        "signing": "signing",
        "interview": "interview",
        "address": "address",
        "sotu": "address",
        "earnings": "earnings",
        "diplomatic": "diplomatic",
    }
    if event_type in _TYPE_MAP:
        return _TYPE_MAP[event_type]

    # --- 2. Speaker heuristic: WH press secretaries always → briefing ---
    speaker = (event.get("speaker") or "").lower()
    _BRIEFING_SPEAKERS = {"leavitt", "mcenany", "psaki", "jean-pierre", "spicer", "sanders"}
    if any(s in speaker for s in _BRIEFING_SPEAKERS):
        return "briefing"

    # --- 3. Fall back to keyword classification on title only (NOT the per-phrase
    #    market question which contains red-herring phrases like "save america act") ---
    notes = event.get("notes") or ""
    context_hint = re.search(r"context=(\w+)", notes)
    context_str = context_hint.group(1) if context_hint else ""

    # Strip market-question boilerplate: "Will X say Y at Z" → keep only Z
    clean_title = event_title
    match = re.match(r"[Ww]ill .+? say .+? (?:at|during) (.+?)[\?]?$", event_title)
    if match:
        clean_title = match.group(1)

    combined = f"{clean_title} {notes} {context_str}"
    return classify_event_format(combined, context_str)


def _extract_event_title(event: dict) -> str:
    """Extract a human-readable event title from market snapshots or kalshi cache."""
    event_id = event.get("event_id", "")
    parts = event_id.split(":", 2)
    event_ticker = parts[2] if len(parts) == 3 else event_id

    # 1. Try market_snapshots.raw_json->raw_api->title (most accurate, works for current events)
    if DB_PATH.exists():
        try:
            conn = _db_connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT raw_json FROM market_snapshots WHERE market_id LIKE ? LIMIT 1",
                (f"{event_ticker}%",),
            ).fetchone()
            conn.close()
            if row:
                snap = json.loads(row["raw_json"])
                raw_api = snap.get("raw_api") or {}
                if isinstance(raw_api, str):
                    raw_api = json.loads(raw_api)
                title = raw_api.get("title", "")
                if title:
                    match = re.match(r"What will .+? say during (.+?)\??$", title)
                    return match.group(1) if match else title
        except Exception:
            pass

    # 2. Try Kalshi market cache JSON
    kalshi_cache = ROOT / "data" / "kalshi_markets.json"
    if kalshi_cache.exists():
        try:
            cache = json.loads(kalshi_cache.read_text(encoding="utf-8"))
            market_list = cache.get("markets", []) if isinstance(cache, dict) else cache
            for m in market_list:
                if m.get("event_ticker") == event_ticker:
                    title = m.get("title", "") or m.get("event_title", "")
                    if title:
                        match = re.match(r"What will .+? say during (.+?)\??$", title)
                        return match.group(1) if match else title
        except Exception:
            pass

    # 3. Fallback: derive something from the ticker
    ticker_clean = re.sub(r"-\d{2}[A-Z]{3}\d{2}$", "", event_ticker)
    return ticker_clean if ticker_clean else event_id


# ── LLM prompt ────────────────────────────────────────────────────────────────

def _build_event_system_prompt() -> str:
    """Build the per-event system prompt from MD context files."""
    try:
        from app.llm_context import build_system_prompt
        prompt = build_system_prompt([
            "mission",
            "per_event_guide",
            "event_formats",
            "trump_patterns",
            "calibration_guide",
        ])
        if prompt:
            return prompt
    except Exception as exc:
        logger.debug("Could not load LLM context files: %s", exc)
    return (
        "You are an expert prediction market analyst specializing in political speech. "
        "Your job is to assess how likely specific phrases are to be spoken at a SPECIFIC event, "
        "given the event's format, participants, and topic — not just the general news cycle. "
        "Be precise. Cite the specific event format constraints that drive your suppression or boost decisions."
    )


EVENT_SYSTEM_PROMPT = _build_event_system_prompt()


def _build_event_prompt(
    event: dict,
    event_title: str,
    event_fmt: str,
    fmt_note: str,
    phrases: list[str],
    base_rates: dict[str, float],
    global_context: str,
) -> str:
    sys.path.insert(0, str(ROOT))
    from app.event_context import event_format_label

    fmt_label = event_format_label(event_fmt)
    speaker_raw = (event.get("speaker") or "unknown").lower()
    speaker = speaker_raw.title()
    state = event.get("speech_state", "unknown")

    # Append a role clarification for known spokesperson-type speakers so the LLM
    # never confuses them with Trump and applies rally-level logic.
    _SPOKESPERSON_ROLES: dict[str, str] = {
        "leavitt": "WH Press Secretary — formal policy language, DHS/immigration focus",
        "mcenany": "WH Press Secretary — formal policy language",
        "psaki":   "WH Press Secretary — formal policy language",
        "jean-pierre": "WH Press Secretary — formal policy language",
        "spicer":  "WH Press Secretary — formal policy language",
        "sanders": "WH Press Secretary — formal policy language",
    }
    speaker_role = next(
        (role for key, role in _SPOKESPERSON_ROLES.items() if key in speaker_raw),
        None,
    )
    speaker_line = f"{speaker} — {speaker_role}" if speaker_role else speaker

    phrase_lines = []
    for p in phrases:
        rate = base_rates.get(p.lower())
        rate_str = f" [hist YES: {rate*100:.0f}%]" if rate is not None else " [hist: unknown]"
        phrase_lines.append(f"  - {p}{rate_str}")
    phrase_list = "\n".join(phrase_lines)

    return f"""EVENT DETAILS:
  Title:   {event_title}
  Speaker: {speaker_line}
  Format:  {fmt_label}
  Status:  {state.upper()}

FORMAT CONTEXT:
{fmt_note}

GLOBAL NEWS CONTEXT (for grounding):
{global_context}

TASK:
For each phrase below, provide TWO types of adjustments for THIS specific event.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART 1 — MULTIPLIERS (adjust the historical base rate)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Output a multiplier for each phrase whose likelihood differs from its historical average
given THIS event's specific format and topic.

Multiplier scale:
  0.1 – 0.3 : STRONG SUPPRESS — highly inappropriate/irrelevant for this format
  0.3 – 0.6 : MODERATE SUPPRESS — unlikely in this format but not impossible
  0.7 – 0.9 : MILD SUPPRESS — slightly less likely than historical for this format
  1.0        : NO CHANGE — omit
  1.1 – 1.3 : MILD BOOST — format makes this slightly more likely
  1.3 – 1.6 : MODERATE BOOST — specifically relevant to this event's topic
  1.6 – 2.0 : STRONG BOOST — central to this specific event (e.g. host country name at summit)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART 2 — P_FLOORS (minimum probability, overrides low base rates)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A p_floor is a minimum probability: regardless of the historical base rate, this phrase
should be AT LEAST this probable given the event's purpose and domain.

Use p_floors ONLY for phrases where the event context STRUCTURALLY elevates the
probability well above the historical base rate. Examples:
- At a DHS swearing-in ceremony: "deport" p_floor=0.65 (DHS's core function)
- At a border security summit: "illegal alien" p_floor=0.70
- At a bill signing for immigration: "wall" p_floor=0.60
- At a sports roundtable: "nfl" p_floor=0.55

Do NOT use p_floors for:
- Generic phrases that might come up (use multipliers instead)
- Phrases that are merely topically relevant but not domain-essential
- Events where base rates are already appropriate

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Critical rules — SPEAKER-SPECIFIC BEHAVIOR:

TRUMP: Event format does NOT predict his vocabulary. Hard data proves:
  "sleepy joe" YES at 92%, "democrat" 64%, "fake news" 66% — across ALL formats.
  Trump uses insults, nicknames, and catchphrases at bill signings, diplomatic
  meetings, and press conferences equally. DO NOT suppress Trump's habitual
  phrases based on event format. Only suppress phrases that are topically
  irrelevant (e.g. "baseball" at a tariff signing) AND have hist YES < 30%.
  For Trump, if hist YES > 50%, the multiplier must be >= 0.90.

LEAVITT: WH Press Secretary. Uses formal policy language. DOES suppress
  Trump-style insults (she won't say "fat slob" or "sleepy joe").
  Focus on DHS/immigration/policy terms. Suppress entertainment/personal terms.

POWELL/FED: Extremely structured FOMC press conferences. Predictable economic
  vocabulary only. Suppress ALL political/personal terms aggressively (0.1x).

MAMDANI: NYC mayoral candidate. Housing, NYPD, transit, budget vocabulary.
  National politics irrelevant — suppress federal/foreign policy terms.

General rules:
- Each adjustment must be grounded in the SPECIFIC format, not general news
- p_floors should be rare — only set them when you are confident the event's
  purpose structurally guarantees an elevated floor (not just relevance)

Return ONLY a JSON object:
{{
  "event_format": "{event_fmt}",
  "topics": ["list", "of", "2-4", "key", "event", "topics"],
  "adjustments": [
    {{
      "phrase": "exact phrase lowercase",
      "multiplier": 0.2,
      "reason": "1-2 sentences: why this format specifically suppresses/boosts this phrase"
    }}
  ],
  "p_floors": {{
    "phrase": {{"p_floor": 0.65, "reason": "DHS Secretary swearing-in: deportation is the core function"}}
  }}
}}

Note: "p_floors" should only contain phrases from the list below where a structural floor applies.
Omit "p_floors" entirely if no phrases warrant a floor.

Phrases to assess:
{phrase_list}"""


# ── LLM call ─────────────────────────────────────────────────────────────────

def _run_llm(prompt: str, client) -> dict:
    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": EVENT_SYSTEM_PROMPT},
                {"role": "user",   "content": prompt},
            ],
            max_completion_tokens=_max_tokens(4000, LLM_MODEL),
            **_llm_extra_kwargs(LLM_MODEL),
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        return json.loads(raw)
    except Exception as exc:
        logger.error("LLM call failed: %s", exc)
        return {}


def _validate_adjustments(raw_adjustments: list, phrase_set: set[str]) -> dict[str, dict]:
    """Validate and normalize LLM output to {phrase: {multiplier, reason}}."""
    result: dict[str, dict] = {}
    for a in raw_adjustments:
        phrase = str(a.get("phrase", "")).lower().strip()
        mult = a.get("multiplier", 1.0)
        reason = str(a.get("reason", ""))[:300]
        if not phrase or phrase not in phrase_set:
            continue
        try:
            mult = float(mult)
        except (TypeError, ValueError):
            continue
        if mult == 1.0:
            continue
        mult = round(max(0.05, min(3.0, mult)), 3)
        result[phrase] = {"multiplier": mult, "reason": reason}
    return result


def _validate_p_floors(
    raw_floors: dict,
    phrase_set: set[str],
    event_fmt: str = "general",
    speaker: str = "",
) -> dict[str, dict]:
    """Validate and normalize LLM p_floors output.

    Accepts either:
      {"phrase": 0.65}
      {"phrase": {"p_floor": 0.65, "reason": "..."}}

    Returns {phrase: {"p_floor": float, "reason": str}}.

    Extra safeguards:
    - briefing/presser speakers are not Trump; cap p_floors at 0.55 to prevent
      LLM from injecting rally-level floors onto policy-constrained spokespeople.
    - diplomatic format: cap at 0.60 (scripted, bilateral agenda).
    """
    # Per-format p_floor ceiling to prevent over-confident floors
    _FMT_CEIL = {
        "briefing": 0.55,
        "presser": 0.60,
        "diplomatic": 0.60,
        "testimony": 0.60,
        "signing": 0.70,
        "address": 0.70,
    }
    p_floor_ceil = _FMT_CEIL.get(event_fmt, 0.92)

    # Known WH press secretaries — their p_floors are capped even tighter
    _SPOKESPERSON_SPEAKERS = {"leavitt", "mcenany", "psaki", "jean-pierre", "spicer", "sanders"}
    if any(s in speaker.lower() for s in _SPOKESPERSON_SPEAKERS):
        p_floor_ceil = min(p_floor_ceil, 0.50)

    result: dict[str, dict] = {}
    if not isinstance(raw_floors, dict):
        return result
    for phrase, val in raw_floors.items():
        key = str(phrase).lower().strip()
        if not key or key not in phrase_set:
            continue
        try:
            if isinstance(val, dict):
                p_fl = float(val.get("p_floor", val.get("value", 0)))
                reason = str(val.get("reason", ""))[:300]
            else:
                p_fl = float(val)
                reason = ""
        except (TypeError, ValueError):
            continue
        # Sanity: floors should be meaningful (> 0.30) and not exceed per-format ceiling
        if p_fl < 0.30 or p_fl > p_floor_ceil:
            logger.debug(
                "p_floor for '%s' clamped: %.2f → exceeds ceiling %.2f for fmt=%s speaker=%s",
                key, p_fl, p_floor_ceil, event_fmt, speaker,
            )
            continue
        result[key] = {"p_floor": round(p_fl, 3), "reason": reason}
    return result


# ── Main analysis ─────────────────────────────────────────────────────────────

def analyze_event(event: dict, client, base_rates: dict[str, float], force: bool = False) -> bool:
    """Run LLM analysis for a single event. Returns True if analysis was performed."""
    event_id = event["event_id"]

    if not force and _is_fresh(event_id):
        logger.info("SKIP %s — signal file is fresh (< %ds old)", event_id, REFRESH_SEC)
        return False

    sys.path.insert(0, str(ROOT))
    from app.event_context import event_format_note

    event_title = _extract_event_title(event)
    event_fmt = _classify_event(event, event_title)
    fmt_note = event_format_note(event_fmt)

    # ── Domain-selective LLM skip ─────────────────────────────────────────────
    # LLM adds noise for deterministic domains (NBA/sports/earnings) where phrase
    # occurrence is driven by game mechanics, not political context.  These events
    # rely on deterministic base rates + arena/certainty overrides only.
    _LLM_SKIP_FORMATS = frozenset([
        "earnings", "nba_broadcast", "sports", "entertainment",
    ])
    _event_type_raw = (event.get("event_type") or "").lower()
    # Also skip by series prefix — catch sports events classified as "general"
    _ticker_lower = event_id.lower().split(":")[-1]  # strip "auto:nba:" prefix
    _is_sports_series = any(_ticker_lower.startswith(p) for p in _SPORTS_SERIES_PREFIXES)
    if event_fmt in _LLM_SKIP_FORMATS or _event_type_raw in _LLM_SKIP_FORMATS or _is_sports_series:
        logger.info(
            "Skipping LLM for event %s (format=%s, type=%s, series_skip=%s) — deterministic domain",
            event_id, event_fmt, _event_type_raw, _is_sports_series,
        )
        return False

    phrases = _load_phrases_for_event(event_id)
    if not phrases:
        logger.warning("No phrases found for event %s — skipping", event_id)
        return False

    # Cap phrase universe — prioritize phrases closest to 0.5 base rate (most uncertain,
    # highest LLM value). Phrases near 0 or 1 don't need LLM context.
    if len(phrases) > MAX_PHRASES_TOTAL:
        base_rates_local = _load_base_rates()
        phrases = sorted(
            phrases,
            key=lambda p: abs((base_rates_local.get(p, 0.5)) - 0.5),
        )[:MAX_PHRASES_TOTAL]

    logger.info(
        "Analyzing event: %s | format=%s | %d phrases",
        event_id, event_fmt, len(phrases),
    )
    logger.info("  Title: %s", event_title)

    global_context = _load_global_signal_context()

    # Batch phrases if many
    all_adjustments: dict[str, dict] = {}
    all_p_floors: dict[str, dict] = {}
    phrase_set = set(phrases)
    # Track topics from the FIRST batch only (bug fix: loop variable `i` was
    # checked at write time, always using the last batch index, writing [] for
    # multi-batch events).
    first_batch_topics: list = []

    for i in range(0, len(phrases), MAX_PHRASES_BATCH):
        batch = phrases[i:i + MAX_PHRASES_BATCH]
        prompt = _build_event_prompt(
            event=event,
            event_title=event_title,
            event_fmt=event_fmt,
            fmt_note=fmt_note,
            phrases=batch,
            base_rates=base_rates,
            global_context=global_context,
        )
        result = _run_llm(prompt, client)
        raw_adj = result.get("adjustments", [])
        batch_adj = _validate_adjustments(raw_adj, phrase_set)
        all_adjustments.update(batch_adj)

        raw_floors = result.get("p_floors", {})
        batch_floors = _validate_p_floors(
            raw_floors, phrase_set,
            event_fmt=event_fmt,
            speaker=event.get("speaker", ""),
        )
        all_p_floors.update(batch_floors)

        # Capture topics from first batch only
        if i == 0:
            first_batch_topics = result.get("topics", [])

    # Log summary
    suppressed = [(p, d) for p, d in all_adjustments.items() if d["multiplier"] < 1.0]
    boosted    = [(p, d) for p, d in all_adjustments.items() if d["multiplier"] > 1.0]
    suppressed.sort(key=lambda x: x[1]["multiplier"])
    boosted.sort(key=lambda x: -x[1]["multiplier"])

    if suppressed:
        logger.info("Top suppressions (%d):", len(suppressed))
        for p, d in suppressed[:8]:
            logger.info("  ↓ %-22s  x%.2f  %s", p, d["multiplier"], d["reason"][:70])
    if boosted:
        logger.info("Top boosts (%d):", len(boosted))
        for p, d in boosted[:5]:
            logger.info("  ↑ %-22s  x%.2f  %s", p, d["multiplier"], d["reason"][:70])
    if all_p_floors:
        logger.info("P_floors set (%d):", len(all_p_floors))
        for p, d in sorted(all_p_floors.items(), key=lambda x: -x[1]["p_floor"])[:8]:
            logger.info("  ⌊ %-22s  %.2f  %s", p, d["p_floor"], d["reason"][:70])

    output = {
        "event_id":    event_id,
        "event_title": event_title,
        "event_format": event_fmt,
        "speaker":     event.get("speaker", ""),
        "speech_state": event.get("speech_state", ""),
        "analyzed_at": datetime.now(tz=timezone.utc).isoformat(),
        "model":       LLM_MODEL,
        "topics":      first_batch_topics,
        "adjustments": all_adjustments,
        "p_floors":    all_p_floors,   # domain-based minimums — do not delete manually
    }
    _write_event_signals(event_id, output)
    return True


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Per-event LLM phrase scoring")
    parser.add_argument("--event-id", help="Analyze a specific event ID only")
    parser.add_argument("--force",    action="store_true", help="Skip freshness check")
    args = parser.parse_args()

    api_key = _get_api_key()
    client  = _client(api_key)
    base_rates = _load_base_rates()
    logger.info("Loaded base rates for %d phrases", len(base_rates))

    events = _load_live_events(event_id=args.event_id)
    if not events:
        if args.event_id:
            logger.error("Event not found: %s", args.event_id)
        else:
            logger.info("No live/scheduled events to analyze")
        return

    analyzed = 0
    for ev in events:
        try:
            did_run = analyze_event(ev, client, base_rates, force=args.force)
            if did_run:
                analyzed += 1
        except Exception as exc:
            logger.error("Failed to analyze event %s: %s", ev.get("event_id"), exc)

    logger.info("Done: %d/%d events analyzed", analyzed, len(events))


if __name__ == "__main__":
    main()
