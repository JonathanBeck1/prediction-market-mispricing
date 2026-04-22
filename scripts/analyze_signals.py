#!/usr/bin/env python3
"""D2 — LLM reasoning layer: phrase probability adjustments via GPT-4o-mini.

Two-step architecture:

  Step 1: Topic extraction
    Feed context (WH schedule, news, Truth Social, trends) to the LLM.
    Ask it: "What are the 5-8 key topics Trump will likely address today?"
    This grounds all per-phrase reasoning in concrete, specific context.

  Step 2: Per-phrase assessment (batched, 60 phrases at a time)
    Feed the extracted topics + phrases to the LLM.
    Ask for: boost multiplier + 2-3 sentence reasoning citing specific evidence.

The output per phrase:
  {
    "phrase": "iran",
    "boost": 1.6,
    "reasoning": "Iran is dominating the news cycle (Google Trends: 59, WH Remarks
                  scheduled today). Trump posted on Truth Social 3h ago about
                  denuclearization. In contexts where Iran-focused events are
                  scheduled, mention rate rises from 45% baseline to ~70%.",
    "evidence": "Trump TS: 'Total denuclearization or NO deal' | Iran trends at 59",
    "signals_used": ["truth_social", "wh_schedule", "google_trends"]
  }

Requires: OPENAI_API_KEY environment variable (add to config/runtime.env).
Output:
  - data/signals.yaml        — llm_boost + llm_reasoning + llm_evidence per phrase
  - data/llm_analysis.json   — full structured output for inspection
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.db import connect as _db_connect  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SIGNAL_CONTEXT_PATH = Path("data/signal_context.json")
SIGNALS_YAML_PATH   = Path("data/signals.yaml")
LLM_ANALYSIS_PATH   = Path("data/llm_analysis.json")
OUTCOMES_PATH       = Path("data/kalshi_outcomes.json")
ENV_PATH            = Path("config/runtime.env")

# Verbatim `[story:xxxxxxxxxxxxxxxx]` tags copied from signal_context (16-hex id).
_STORY_TAG_RE = re.compile(r"\[story:([a-f0-9]{16})\]", re.IGNORECASE)


def _extract_story_hashes(*texts: str) -> list[str]:
    found: set[str] = set()
    for t in texts:
        for m in _STORY_TAG_RE.findall(t or ""):
            found.add(m.lower())
    return sorted(found)
DB_PATH             = Path("data/edge.db")

LLM_MODEL        = os.getenv("LLM_SIGNAL_MODEL", "gpt-4o-mini")
BATCH_SIZE       = 60   # phrases per Step 2 call

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

# Hard cap on LLM boost based on historical base YES rate.
# A phrase that rarely gets mentioned (low base rate) shouldn't be boosted
# as aggressively as a phrase Trump says all the time — the news cycle can
# only move a low-frequency phrase so far above its historical floor.
_BOOST_CAP_BY_BASE_RATE = [
    (0.35, 1.15),   # base YES rate < 35%  → max boost 1.15x
    (0.45, 1.25),   # base YES rate < 45%  → max boost 1.25x
    (0.55, 1.40),   # base YES rate < 55%  → max boost 1.40x
    (1.00, 1.80),   # base YES rate >= 55% → max boost 1.80x
]


# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

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
        logger.error(
            "OPENAI_API_KEY not set.\n"
            "Add to config/runtime.env:  OPENAI_API_KEY=sk-...\n"
            "Or export it:               export OPENAI_API_KEY=sk-..."
        )
        sys.exit(1)
    return key


def _client(api_key: str):
    from openai import OpenAI
    return OpenAI(api_key=api_key)


# ──────────────────────────────────────────────────────────────────────────────
# Context builder
# ──────────────────────────────────────────────────────────────────────────────

def _load_historical_yes_rates() -> dict[str, float]:
    """Return {phrase_lower: yes_rate} from kalshi_outcomes.json."""
    if not OUTCOMES_PATH.exists():
        return {}
    try:
        data = json.loads(OUTCOMES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    counts: dict[str, list[int, int]] = {}  # phrase → [yes_count, total]
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
        if total >= 3  # require at least 3 samples
    }


def _cap_boost_by_base_rate(boost: float, base_rate: float | None) -> float:
    """Apply hard cap on LLM boost based on historical YES rate."""
    if base_rate is None:
        return min(boost, 1.30)  # unknown history → conservative cap
    for threshold, cap in _BOOST_CAP_BY_BASE_RATE:
        if base_rate < threshold:
            return min(boost, cap)
    return boost


def _load_live_events() -> list[dict]:
    """Return currently live/scheduled events from the DB for event-specific LLM context."""
    if not DB_PATH.exists():
        return []
    try:
        conn = _db_connect(DB_PATH)
        rows = conn.execute(
            "SELECT event_id, event_type, notes, speech_state "
            "FROM events WHERE speech_state IN ('live','scheduled') "
            "ORDER BY CASE speech_state WHEN 'live' THEN 0 ELSE 1 END, created_at DESC "
            "LIMIT 8"
        ).fetchall()
        conn.close()
        return [
            {"event_id": r[0], "event_type": r[1], "notes": r[2] or "", "state": r[3]}
            for r in rows
        ]
    except Exception:
        return []


def _build_raw_context(ctx: dict) -> str:
    """Build a compact, rich context string from signal_context.json."""
    lines: list[str] = []
    now_str = ctx.get("fetched_at", "")[:16]
    lines.append(f"=== CONTEXT AS OF {now_str} UTC ===\n")

    # ── Live / scheduled Kalshi events (highest priority for phrase assessment) ──
    live_events = _load_live_events()
    if live_events:
        live = [e for e in live_events if e["state"] == "live"]
        sched = [e for e in live_events if e["state"] == "scheduled"]
        if live:
            lines.append("CURRENTLY LIVE KALSHI EVENTS (phrase bets are OPEN NOW):")
            for e in live:
                desc = f"  [LIVE] {e['event_type'].upper()} — {e['event_id']}"
                if e["notes"]:
                    desc += f" | {e['notes'][:120]}"
                lines.append(desc)
            lines.append("")
        if sched:
            lines.append("UPCOMING KALSHI EVENTS:")
            for e in sched:
                desc = f"  [SCHEDULED] {e['event_type'].upper()} — {e['event_id']}"
                if e["notes"]:
                    desc += f" | {e['notes'][:80]}"
                lines.append(desc)
            lines.append("")

    # WH scheduled events (most actionable)
    wh_sched = ctx["sources"].get("whitehouse_schedule", [])[:8]
    if wh_sched:
        lines.append("WHITE HOUSE SCHEDULED EVENTS:")
        for ev in wh_sched:
            kw = ", ".join(ev.get("keywords", [])[:5])
            lines.append(f"  [{ev.get('event_type','?').upper()}] {ev.get('title','')[:100]}"
                         + (f" | keywords: {kw}" if kw else ""))
        lines.append("")

    # WH official RSS headlines
    wh_rss = ctx["sources"].get("whitehouse_rss", [])[:6]
    if wh_rss:
        lines.append("WHITE HOUSE NEWS: (each line has a [story:...] id — copy into evidence when citing)")
        for h in wh_rss:
            sid = h.get("source_story_hash") or "?"
            lines.append(f"  - [story:{sid}] {h.get('title','')[:100]}")
        lines.append("")

    # Truth Social posts (highest signal — these preview speech content)
    ts_posts = ctx["sources"].get("truth_social", [])[:8]
    if ts_posts:
        import datetime as _dt
        _now = _dt.datetime.now(_dt.timezone.utc)
        lines.append(
            "TRUMP TRUTH SOCIAL POSTS: (copy [story:...] into evidence when citing that post)"
        )
        for p in ts_posts:
            content = (p.get("content") or p.get("text") or p.get("body", "")).strip()
            if not content:
                continue
            ts_sid = p.get("source_story_hash") or "?"
            # Use posted_at (canonical schema); fall back to created_at for legacy entries
            raw_ts = p.get("posted_at") or p.get("created_at") or ""
            age_label = "unknown date"
            if raw_ts:
                try:
                    dt = _dt.datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                    age_h = (_now - dt).total_seconds() / 3600
                    if age_h < 2:
                        age_label = f"{age_h*60:.0f}m ago"
                    elif age_h < 48:
                        age_label = f"{age_h:.1f}h ago"
                    else:
                        age_label = f"{age_h/24:.1f}d ago — treat as background context only"
                except Exception:
                    age_label = raw_ts[:16]
            lines.append(f"  [{age_label}] [story:{ts_sid}] {content[:200]}")
        lines.append("")

    # Google News
    gn = ctx["sources"].get("google_news", [])[:12]
    if gn:
        lines.append("RECENT HEADLINES (AP/Reuters/NYT): (copy [story:...] into evidence when citing)")
        for h in gn:
            sid = h.get("source_story_hash") or "?"
            lines.append(f"  - [story:{sid}] {h.get('title','')[:100]}")
        lines.append("")

    # Google Trends
    trends = ctx["sources"].get("google_trends", [])[:10]
    if trends:
        top = [f"{t['keyword']}({t['interest']})" for t in trends]
        lines.append(f"GOOGLE TRENDS (last 24h, scale 0-100): {', '.join(top)}\n")

    # Recent speech transcripts (highest-quality signal for language patterns)
    transcripts = ctx["sources"].get("recent_transcripts", [])[:3]
    if transcripts:
        lines.append("RECENT SPEECH TRANSCRIPTS (direct language evidence):")
        for t in transcripts:
            age = t.get("age_days", "?")
            speaker = t.get("speaker", "unknown").upper()
            fname = t.get("file", "")
            excerpt = t.get("excerpt", "")[:1500]
            lines.append(f"  [{speaker} — {fname} — {age}d ago]")
            lines.append(f"  {excerpt}")
            lines.append("")
        lines.append(
            "NOTE: Transcript language is the STRONGEST signal. If Trump used a phrase "
            "in a recent transcript, boost it. If he didn't use a phrase he usually does, "
            "that absence is a mild suppression signal.\n"
        )

    # Per-phrase LLM signal history (feedback loop)
    phrase_history = ctx.get("phrase_signal_history", {})
    if phrase_history:
        lines.append("YOUR PAST LLM SIGNAL ACCURACY (by phrase):")
        lines.append("RULE: If boost WR < 40% with 3+ bets → this phrase historically beats your boosts, be cautious.")
        lines.append("RULE: If boost WR > 70% with 3+ bets → your boosts for this phrase have been excellent, trust them.")
        lines.append("RULE: If suppress WR < 40% → your suppression for this phrase was wrong, reconsider suppression.")

        # Separate into reliable boosters, unreliable boosters, and bad suppressions
        reliable_boosts = []
        poor_boosts = []
        poor_suppresses = []
        for phrase, stats in phrase_history.items():
            bn, bwr = stats.get("boost_n", 0), stats.get("boost_wr")
            sn, swr = stats.get("suppress_n", 0), stats.get("suppress_wr")
            if bn >= 3 and bwr is not None:
                if bwr >= 0.65:
                    reliable_boosts.append((phrase, bn, bwr))
                elif bwr < 0.40:
                    poor_boosts.append((phrase, bn, bwr))
            if sn >= 3 and swr is not None and swr < 0.40:
                poor_suppresses.append((phrase, sn, swr))

        if reliable_boosts:
            lines.append("  HISTORICALLY ACCURATE BOOSTS (trust these):")
            for ph, n, wr in sorted(reliable_boosts, key=lambda x: -x[2])[:8]:
                lines.append(f"    ✓ {ph}: {int(wr*100)}% win rate on {n} boosted bets")
        if poor_boosts:
            lines.append("  HISTORICALLY POOR BOOSTS (do NOT boost these aggressively):")
            for ph, n, wr in sorted(poor_boosts, key=lambda x: x[2])[:8]:
                lines.append(f"    ✗ {ph}: only {int(wr*100)}% win rate on {n} boosted bets — be conservative")
        if poor_suppresses:
            lines.append("  HISTORICALLY BAD SUPPRESSIONS (do NOT suppress these):")
            for ph, n, wr in sorted(poor_suppresses, key=lambda x: x[2])[:6]:
                lines.append(f"    ✗ {ph}: only {int(wr*100)}% win rate on {n} suppressed bets — your suppression backfired")

        lines.append("")

    # Current Kalshi market prices (what the market currently prices each phrase)
    kalshi_prices = ctx["sources"].get("kalshi_market_prices", [])
    if kalshi_prices:
        lines.append("CURRENT KALSHI MARKET PRICES (yes_ask cents on the dollar):")
        lines.append("IMPORTANT: Do NOT boost a phrase that is already priced high (>0.80) — the market has already priced it in.")
        lines.append("Focus boosts on underpriced phrases (priced 0.10-0.50) where news strongly suggests YES.")
        for m in kalshi_prices[:50]:
            price_pct = int(round(m["yes_ask"] * 100))
            series = m.get("series", "").replace("KXTRUMPMENTION", "MENTION").replace("KXTRUMPSAY", "SAY")
            oi = m.get("open_interest")
            oi_str = f" OI={oi}" if oi else ""
            lines.append(f"  {m['phrase']}: {price_pct}¢{oi_str} [{series}]")
        lines.append("")

    # Polymarket prices (independent crowd calibration)
    poly_prices = ctx["sources"].get("polymarket_prices", [])
    if poly_prices:
        lines.append("POLYMARKET CROWD PRICES (independent prediction market — high conf only):")
        lines.append("These are independent crowd forecasts for Trump speech phrases. Use as calibration anchor.")
        for p in poly_prices[:25]:
            price_pct = int(round(p["yes_price"] * 100))
            conf_label = "HIGH" if p["confidence"] >= 0.8 else "MED"
            tf = p.get("timeframe", "?")[:7]
            lines.append(f"  {p['phrase']}: {price_pct}¢ [conf={conf_label} tf={tf}]")
        lines.append("NOTE: When Polymarket prices a phrase at 60%+ and Kalshi prices it at 10%, that gap is a signal.")
        lines.append("")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Step 1: Topic extraction
# ──────────────────────────────────────────────────────────────────────────────

def _build_topic_system_prompt() -> str:
    """Build the topic extraction system prompt from MD context files."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from app.llm_context import build_system_prompt
        prompt = build_system_prompt(["mission", "trump_patterns"])
        if prompt:
            return prompt + (
                "\n\n--- CURRENT TASK ---\n"
                "You are extracting the 5-8 most likely topics the speaker will address. "
                "Be concrete and evidence-based. Reference specific sources when available."
            )
    except Exception as exc:
        logger.debug("Could not load LLM context files: %s", exc)
    return (
        "You are a senior political intelligence analyst specializing in predicting "
        "what topics a US president will address in his next public appearance. "
        "Be concrete and evidence-based. Reference specific sources when available."
    )


TOPIC_EXTRACTION_SYSTEM = _build_topic_system_prompt()

def _step1_extract_topics(context_str: str, client, live_events: list[dict] | None = None) -> list[dict]:
    """Extract 5-8 key topics Trump will likely address today, grounded in live events."""
    event_directive = ""
    if live_events:
        live_str = "; ".join(
            f"{e['event_type']} ({e['event_id']}" + (f": {e['notes'][:60]}" if e['notes'] else "") + ")"
            for e in live_events[:4]
        )
        event_directive = (
            f"\n\nCRITICAL: The following Kalshi prediction markets are LIVE right now — "
            f"phrase bets are open and bettors need assessments specifically for these events: {live_str}. "
            f"Your topic list MUST include topics relevant to these live events first."
        )

    prompt = f"""{context_str}
{event_directive}
ACTIVE PREDICTION MARKET SPEAKERS (only these — do NOT generate topics for others):
  - Donald Trump (speeches, signings, rallies, press conferences)
  - Karoline Leavitt (White House press briefings)
  - Zohran Mamdani (NYC mayoral campaign events)
  - Jerome Powell / Federal Reserve (FOMC press conferences, testimony)
  Sports markets (NBA, NCAAB, MLB, MMA) are broadcast-phrase markets —
  do NOT include sports topics; they are unaffected by news cycle analysis.

Based on this intelligence, identify the 5-8 most likely topics Trump, Leavitt,
Mamdani, or Powell will address in their next public appearance within 12-24 hours.
Focus on topics directly relevant to the active speakers above.

For each topic, cite the specific evidence that makes it likely.

Return ONLY a JSON object like:
{{
  "topics": [
    {{
      "topic": "Iran nuclear negotiations",
      "speaker": "trump",
      "probability": "high",
      "evidence": "Trump TS post 3h ago + WH Remarks scheduled + Iran trends at 59",
      "likely_phrases": ["iran", "nuclear", "deal", "sanctions"],
      "reasoning": "2-3 sentence explanation of why this topic is likely today"
    }}
  ]
}}"""

    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": TOPIC_EXTRACTION_SYSTEM},
                {"role": "user",   "content": prompt},
            ],
            max_completion_tokens=_max_tokens(4000, LLM_MODEL),
            **_llm_extra_kwargs(LLM_MODEL),
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        topics = parsed.get("topics", [])
        logger.info("Step 1: extracted %d key topics", len(topics))
        for t in topics:
            logger.info("  [%s] %s", t.get("probability","?").upper(), t.get("topic",""))
        return topics
    except Exception as exc:
        logger.error("Step 1 (topic extraction) failed: %s", exc)
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Step 2: Per-phrase assessment
# ──────────────────────────────────────────────────────────────────────────────

def _build_system_prompt() -> str:
    """Build the system prompt from MD context files."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from app.llm_context import build_system_prompt
        prompt = build_system_prompt(["mission", "global_signals_guide", "calibration_guide", "trump_patterns"])
        if prompt:
            return prompt
    except Exception as exc:
        logger.debug("Could not load LLM context files: %s", exc)

    # Fallback if MD files not found
    return (
        "You are a prediction market analyst. Given today's key political topics "
        "and a list of phrases from betting markets, assess the probability "
        "adjustment for each phrase. Be specific — cite the exact news item, "
        "post, or trend that drives your estimate. "
        "Suppression (boost < 1.0) is AS IMPORTANT as boosting — actively suppress phrases "
        "that today's news cycle makes LESS likely than usual. Do not skip suppression."
    )


PHRASE_ASSESSMENT_SYSTEM = _build_system_prompt()

def _step2_assess_phrases(
    topics: list[dict],
    phrases: list[str],
    context_str: str,
    client,
    base_rates: dict[str, float] | None = None,
) -> list[dict]:
    """Assess each phrase against today's extracted topics."""
    topics_summary = "\n".join(
        f"  [{t.get('probability','?').upper()}] {t.get('topic','')} "
        f"— {t.get('evidence','')} "
        f"(likely phrases: {', '.join(t.get('likely_phrases',[])[:6])})"
        for t in topics
    )

    # Include historical YES rate next to each phrase so the LLM calibrates
    # boosts relative to the phrase's actual frequency, not just news relevance.
    if base_rates:
        phrase_lines = []
        for p in phrases:
            rate = base_rates.get(p.lower())
            rate_str = f" [hist YES: {rate*100:.0f}%]" if rate is not None else " [hist: unknown]"
            phrase_lines.append(f"  - {p}{rate_str}")
        phrase_list = "\n".join(phrase_lines)
    else:
        phrase_list = "\n".join(f"  - {p}" for p in phrases)

    prompt = f"""TODAY'S KEY TOPICS (extracted from live context):
{topics_summary}

FULL CONTEXT SUMMARY:
{context_str[:4000]}

TASK: For each phrase, return a probability boost multiplier grounded in
the phrase's HISTORICAL YES RATE (shown in brackets as "hist YES: X%").

═══ CRITICAL: SPEAKER BEHAVIOR PROFILES ═══

TRUMP — FORMAT-INDEPENDENT PHRASES (DO NOT SUPPRESS THESE):
Trump uses certain phrases at virtually every appearance regardless of format
(signings, rallies, press conferences, bilateral meetings). The historical data
proves this — event format does NOT predict whether Trump says these:
  • "sleepy joe" → 92% YES across ALL event types (signings included)
  • "democrat" → 64% YES across ALL event types
  • "transgender" → 93% YES in KXTRUMPSAY (he says it at every event)
  • "fake news" → 66% YES across ALL formats
  • "radical left" → 20% YES (low but format-independent — same at rally or signing)
  • "witch hunt", "crooked", "rigged election" → habitual, not rally-specific
  ➤ RULE: If hist YES > 50%, NEVER suppress below 0.90x regardless of format.
  ➤ RULE: If hist YES > 80%, treat as near-certain (boost or leave at 1.0).
  ➤ RULE: Phrases like insults and nicknames are NOT "rally-specific" for Trump.
     Trump insults people at bill signings, diplomatic events, and funerals.
     The data proves this. Trust the historical rate, not format assumptions.

TRUMP — TOPICAL PHRASES (these ARE format-sensitive):
  • "tariff", "iran", "china", "oil", "nuclear" → boost when on today's agenda
  • "shutdown", "economy", "stock market" → boost when WH schedule is relevant
  These can be boosted OR suppressed based on today's news cycle.

LEAVITT (White House Press Secretary):
  • Formal, structured Q&A — she does NOT use Trump's personal catchphrases
  • "illegal alien" 75%, "border" 67%, "ice" 67% — her high-frequency policy terms
  • "radical left" 0%, "crypto" 0%, "stock market" 8% — genuinely low for briefings
  • Suppress Trump-style nicknames/insults for Leavitt (she won't say "fat slob")
  • Boost DHS/immigration/policy terms when on today's WH schedule

POWELL / FED:
  • Extremely structured FOMC press conferences with predictable vocabulary
  • Focus on: inflation, rates, balance sheet, labor market, projections
  • Will NEVER say political phrases — suppress all political terms to 0.1x
  • Only boost/suppress based on specific FOMC statement language

MAMDANI (NYC Mayoral Campaign):
  • "home" 91%, "safe" 71%, "afford" 59% — his high-frequency terms
  • NYC-local vocabulary — housing, NYPD, transit, budget
  • Iran/tariff/federal policy are irrelevant to Mamdani events

═══ CALIBRATION RULES ═══

Boost scale:
- 1.3-1.6: phrase DIRECTLY tied to a HIGH-probability topic AND hist YES > 50%
- 1.1-1.3: directly tied to HIGH topic with hist < 50%, OR tangential with hist > 50%
- 1.05-1.15: tangentially related, hist YES < 45%
- 1.0: no clear signal (OMIT from response)
- 0.90-0.95: mildly less likely than usual (only for phrases with hist < 40%)
- 0.80-0.90: strong evidence topic will be avoided AND hist < 30%

SUPPRESSION FLOOR RULES:
- NEVER suppress a phrase below 0.85x if its hist YES > 50%
- NEVER suppress a phrase below 0.70x if its hist YES > 30%
- Only suppress below 0.50x if hist YES is < 10% AND no evidence at all
- When in doubt, leave at 1.0 — our data shows suppression hurts more than it helps

LIVE TRADING DATA (from our actual bets):
- LLM_BOOST signals: 28% win rate, -$1.93 PnL → BE CONSERVATIVE WITH BOOSTS
- LLM_SUPPRESS signals: 50% win rate → suppression is only neutral, not helpful
- NO_LLM signals: 42% win rate → baseline without LLM interference
- CONCLUSION: Only boost with HIGH confidence + direct evidence. Default to 1.0.

REQUIRED: Before outputting each multiplier, complete the 4-step checklist:
  STEP 1: What speaker is this phrase for? Apply that speaker's behavioral profile.
  STEP 2: Base rate — is my multiplier proportionate to the historical YES rate?
  STEP 3: Direct evidence — do I have a SPECIFIC source (transcript hit, TS post)?
  STEP 4: Would I bet real money on this adjustment? If not, output 1.0.

For each phrase you assess (boost != 1.0), return:
{{
  "phrase": "exact phrase lowercase",
  "boost": 1.3,
  "boost_low": 1.15,
  "boost_high": 1.45,
  "confidence": "high" | "medium" | "low",
  "direct_evidence": true | false,
  "reasoning": "2-3 sentences: cite specific source + note historical base rate + explain adjustment",
  "evidence": "Direct quote or data point; MUST include verbatim [story:xxxxxxxxxxxxxxxx] from context when citing a headline or Truth Social line",
  "signals_used": ["truth_social" | "wh_schedule" | "google_news" | "google_trends" | "wh_rss" | "transcript"],
  "topic": "which key topic this phrase connects to"
}}

Confidence rules:
- "high": you have a transcript hit, TS post, or WH schedule title match for this EXACT phrase
- "medium": you have indirect but credible signals (news headlines, trends, event topic match)
- "low": circumstantial only — topic is in the news but no direct phrase evidence

When confidence = "low", the system caps your boost at 1.10x maximum.
When confidence = "medium", the system averages boost_low and boost.

Return ONLY a JSON object: {{"assessments": [...]}}
Only include phrases where boost != 1.0. Omit neutral phrases entirely.
Prefer fewer, higher-confidence assessments over many low-confidence ones.

Phrases to assess:
{phrase_list}"""

    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": PHRASE_ASSESSMENT_SYSTEM},
                {"role": "user",   "content": prompt},
            ],
            max_completion_tokens=_max_tokens(3000, LLM_MODEL),
            **_llm_extra_kwargs(LLM_MODEL),
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        assessments = parsed.get("assessments", [])
        # Validate structure and apply confidence-based conservatism + base-rate caps
        valid = []
        for a in assessments:
            phrase     = str(a.get("phrase", "")).lower().strip()
            boost      = float(a.get("boost", 1.0))
            boost_low  = float(a.get("boost_low", boost))
            boost_high = float(a.get("boost_high", boost))
            confidence = str(a.get("confidence", "medium")).lower()
            direct_ev  = bool(a.get("direct_evidence", False))

            if not phrase or phrase not in {p.lower() for p in phrases}:
                continue
            if boost == 1.0:
                continue

            # Apply conservative end of range when confidence is not high:
            # - "low" confidence → use boost_low (safe floor)
            # - "medium" confidence → midpoint of [boost_low, boost]
            # - "high" or direct evidence → use stated boost
            if boost > 1.0:
                # Boosts: be conservative
                if confidence == "low" or not direct_ev:
                    effective_boost = boost_low if boost_low < boost else boost * 0.80
                    effective_boost = max(1.0, min(effective_boost, 1.10))  # low conf → cap at 1.10x
                elif confidence == "medium":
                    effective_boost = (boost_low + boost) / 2.0
                else:
                    effective_boost = boost
            else:
                # Suppressions: high confidence in suppression is fine; be less conservative
                if confidence == "low":
                    # Low confidence suppress → pull back toward neutral
                    effective_boost = (boost + 1.0) / 2.0
                else:
                    effective_boost = boost

            # Hard cap: boost cannot exceed what's appropriate for base rate
            if base_rates is not None and effective_boost > 1.0:
                base_rate = base_rates.get(phrase)
                effective_boost = _cap_boost_by_base_rate(effective_boost, base_rate)

            # Hard FLOOR: suppression cannot go below safe level for high-freq phrases
            if base_rates is not None and effective_boost < 1.0:
                base_rate = base_rates.get(phrase)
                if base_rate is not None:
                    if base_rate > 0.50:
                        effective_boost = max(effective_boost, 0.85)
                    elif base_rate > 0.30:
                        effective_boost = max(effective_boost, 0.70)

            final_boost = round(max(0.05, min(2.5, effective_boost)), 3)
            if final_boost == 1.0:
                continue  # conservatism collapsed it to neutral — skip

            reasoning_txt = str(a.get("reasoning", ""))[:500]
            evidence_txt = str(a.get("evidence", ""))[:200]
            story_hashes = _extract_story_hashes(evidence_txt, reasoning_txt)

            valid.append({
                "phrase":          phrase,
                "boost":           final_boost,
                "boost_stated":    round(boost, 3),
                "confidence":      confidence,
                "direct_evidence": direct_ev,
                "reasoning":       reasoning_txt,
                "evidence":        evidence_txt,
                "signals_used":    list(a.get("signals_used", [])),
                "topic":           str(a.get("topic", ""))[:80],
                "source_story_hashes": story_hashes,
            })
        return valid
    except Exception as exc:
        logger.error("Step 2 (phrase assessment) batch failed: %s", exc)
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Signals YAML update
# ──────────────────────────────────────────────────────────────────────────────

def _load_signals_yaml() -> dict:
    if not SIGNALS_YAML_PATH.exists():
        return {"signals": []}
    try:
        raw = yaml.safe_load(SIGNALS_YAML_PATH.read_text()) or {}
        return raw if isinstance(raw, dict) else {"signals": []}
    except Exception:
        return {"signals": []}


def _update_signals(assessments: list[dict], signals_data: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    existing: dict[str, dict] = {
        sig.get("phrase", "").lower(): sig
        for sig in signals_data.get("signals", [])
        if sig.get("phrase")
    }

    assessed_phrases: set[str] = set()
    updated = 0
    for a in assessments:
        phrase = a["phrase"]
        assessed_phrases.add(phrase)
        if phrase not in existing:
            existing[phrase] = {"phrase": phrase, "news_pressure": 1.0, "x_buzz": 1.0}
        existing[phrase]["llm_boost"]          = a["boost"]
        existing[phrase]["llm_boost_stated"]   = a.get("boost_stated", a["boost"])
        existing[phrase]["llm_confidence"]     = a.get("confidence", "medium")
        existing[phrase]["llm_direct_ev"]      = a.get("direct_evidence", False)
        existing[phrase]["llm_reasoning"]      = a["reasoning"]
        existing[phrase]["llm_evidence"]       = a["evidence"]
        existing[phrase]["llm_topic"]          = a["topic"]
        existing[phrase]["llm_signals"]        = a["signals_used"]
        existing[phrase]["source_story_hashes"] = a.get("source_story_hashes") or []
        existing[phrase]["updated_at"]         = now
        updated += 1

    # Reset stale llm_boost to 1.0 for phrases NOT in this run's assessments.
    # Previous behavior kept old boost values forever, causing stale signals
    # from broken LLM runs to persist and affect scoring indefinitely.
    reset_count = 0
    for phrase, sig in existing.items():
        if phrase not in assessed_phrases and sig.get("llm_boost", 1.0) != 1.0:
            sig["llm_boost"] = 1.0
            sig["llm_boost_stated"] = 1.0
            sig["llm_confidence"] = "expired"
            sig["llm_reasoning"] = "reset: not assessed in latest LLM run"
            sig["updated_at"] = now
            reset_count += 1

    signals_data["signals"] = list(existing.values())
    logger.info("Updated %d phrases, reset %d stale boosts in signals.yaml", updated, reset_count)
    return signals_data


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    api_key = _get_api_key()

    if not SIGNAL_CONTEXT_PATH.exists():
        logger.error("data/signal_context.json not found — run fetch_signals.py first")
        sys.exit(1)

    ctx = json.loads(SIGNAL_CONTEXT_PATH.read_text(encoding="utf-8"))
    phrases = ctx.get("phrase_universe", [])
    if not phrases:
        logger.warning("No phrases in signal_context.json")
        return

    context_str = _build_raw_context(ctx)
    client = _client(api_key)

    # Load historical YES rates to calibrate LLM boosts
    base_rates = _load_historical_yes_rates()
    logger.info("Loaded historical YES rates for %d phrases", len(base_rates))

    # Load live/scheduled events so the LLM focuses on active markets
    live_events = _load_live_events()
    if live_events:
        live = [e for e in live_events if e["state"] == "live"]
        sched = [e for e in live_events if e["state"] == "scheduled"]
        logger.info(
            "Live events for LLM context: %d live, %d scheduled",
            len(live), len(sched),
        )
        for e in live:
            logger.info("  [LIVE] %s — %s", e["event_type"], e["event_id"])

    logger.info("=== analyze_signals: Step 1 — topic extraction ===")
    topics = _step1_extract_topics(context_str, client, live_events=live_events)

    if not topics:
        logger.warning("No topics extracted — skipping phrase assessment")
        return

    logger.info("=== analyze_signals: Step 2 — phrase assessment (%d phrases) ===", len(phrases))
    all_assessments: list[dict] = []
    for i in range(0, len(phrases), BATCH_SIZE):
        batch = phrases[i:i + BATCH_SIZE]
        batch_num = i // BATCH_SIZE + 1
        total_batches = (len(phrases) + BATCH_SIZE - 1) // BATCH_SIZE
        logger.info("  Batch %d/%d (%d phrases)…", batch_num, total_batches, len(batch))
        batch_result = _step2_assess_phrases(topics, batch, context_str, client, base_rates)
        all_assessments.extend(batch_result)
        logger.info("  → %d non-neutral assessments", len(batch_result))

    logger.info("Total assessments: %d / %d phrases", len(all_assessments), len(phrases))

    # Save full analysis
    analysis = {
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "model": LLM_MODEL,
        "context_fetched_at": ctx.get("fetched_at", ""),
        "phrases_evaluated": len(phrases),
        "topics": topics,
        "assessments": all_assessments,
    }
    LLM_ANALYSIS_PATH.write_text(
        json.dumps(analysis, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info("Wrote full analysis to %s", LLM_ANALYSIS_PATH)

    # Update signals.yaml
    signals_data = _load_signals_yaml()
    signals_data = _update_signals(all_assessments, signals_data)
    SIGNALS_YAML_PATH.write_text(
        yaml.dump(signals_data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    logger.info("Updated %s", SIGNALS_YAML_PATH)

    # Print summary
    boosted   = sorted([a for a in all_assessments if a["boost"] > 1.0], key=lambda x: -x["boost"])
    suppressed = sorted([a for a in all_assessments if a["boost"] < 1.0], key=lambda x: x["boost"])

    if boosted:
        logger.info("\nTop LLM boosts:")
        for a in boosted[:10]:
            logger.info(
                "  ↑ %-22s  boost=+%.2fx  [%s]",
                a["phrase"], a["boost"], a.get("topic", "")[:40],
            )
            logger.info("    %s", a["reasoning"][:100])
            if a.get("evidence"):
                logger.info("    Evidence: %s", a["evidence"][:80])

    if suppressed:
        logger.info("\nTop LLM suppressions:")
        for a in suppressed[:5]:
            logger.info(
                "  ↓ %-22s  boost=%.2fx  %s",
                a["phrase"], a["boost"], a["reasoning"][:80],
            )


if __name__ == "__main__":
    main()
