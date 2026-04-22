#!/usr/bin/env python3
"""Process Trump's Truth Social posts and update signals.yaml with phrase boosts.

This script goes beyond simple keyword matching — it reads each post for MEANING:
  1. EVENT TIMING — is this pre-speech (bet now) or post-event (too late)?
  2. TOPIC CLUSTERS — "tariff" implies "trade", "deal", "china"; not just the word
  3. NEGATION — "we are NOT going to do X" dampens X, not boosts it
  4. CONTEXT TYPE — "signing tomorrow" → all signing-context phrases boosted
  5. EMPHASIS signals — ALL CAPS words indicate what Trump intends to hammer on

FLOW:
  1. OpenClaw (or manual paste via add_truth_social_post.py) writes posts to
     data/truth_social_posts.json
  2. This script runs every 15 minutes via MaintenanceRunner
  3. Phrase boosts propagate to scoring engine on next cycle

INPUT SCHEMA (data/truth_social_posts.json):
{
  "fetched_at": "2026-03-12T14:30:00Z",
  "posts": [
    {
      "id": "123456789",
      "content": "We're going to DRILL BABY DRILL! America First! Tariffs NOW!",
      "posted_at": "2026-03-12T12:30:00Z",
      "url": "https://truthsocial.com/@realDonaldTrump/posts/123456789"
    }
  ]
}

BOOST LOGIC (time-decay × caps emphasis, capped at 1.50):
- Post < 2h old:  up to 1.50x  (direct pre-speech signal)
- Post < 6h old:  up to 1.35x  (event day)
- Post < 12h old: up to 1.20x  (warm)
- Post < 24h old: up to 1.10x  (mild)
- Post 1-7d old:  up to 1.05x  (background — useful for weekly/monthly markets)
- Post > 7d old:  1.02x         (long tail — monthly market baseline)
ALL CAPS words: +0.10x bonus

NEGATION: phrases immediately preceded by NOT/NEVER/NO get a DAMP signal (0.80x)
  "we are NOT going to do tariffs" → tariff: 0.80x (not 1.50x)

TOPIC CLUSTER EXPANSION: matching one phrase in a cluster boosts related phrases:
  "tariff" post → also boosts "trade", "deal", "china" (at 70% of direct strength)

CONTEXT TYPE: post implies a speech type → all phrases common in that type are boosted:
  "signing tomorrow" → signing context → "executive order", "ceremony" etc. get +0.05x

Usage:
    python3 scripts/process_truth_social.py          # normal run
    python3 scripts/process_truth_social.py --dry-run  # print changes, no write
    python3 scripts/process_truth_social.py --verbose  # show full post analysis
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError as exc:
    raise SystemExit("PyYAML required: pip install pyyaml") from exc

sys.path.insert(0, str(Path(__file__).parent.parent))

POSTS_PATH = Path("data/truth_social_posts.json")
MARKETS_PATH = Path("data/kalshi_markets.json")
SIGNALS_PATH = Path("data/signals.yaml")

MAX_BOOST = 1.50
MIN_BOOST = 1.02
CAPS_BONUS = 0.10
CLUSTER_SCALE = 0.70   # implied phrases get 70% of the direct match boost
CONTEXT_BOOST = 0.05   # flat boost for all phrases common in inferred context type
NEGATION_DAMP = 0.80   # multiply down if phrase is negated in post

# ─── Time-decay windows ───────────────────────────────────────────────────────
# (max_hours_old, max_multiplier)
# Keep posts for 7+ days — weekly/monthly markets need that history
DECAY_WINDOWS = [
    (2,    1.50),
    (6,    1.35),
    (12,   1.20),
    (24,   1.10),
    (72,   1.07),   # 1–3 days: still relevant for weekly markets
    (168,  1.05),   # 3–7 days: background for weekly/monthly markets
]
DEFAULT_MULT = 1.02    # posts older than 7 days (long tail for monthly markets)

# ─── Topic clusters ───────────────────────────────────────────────────────────
# When a phrase from the left column appears in a post, all phrases in its
# "implies" list get a secondary boost at CLUSTER_SCALE of the direct strength.
TOPIC_CLUSTERS: dict[str, list[str]] = {
    # Trade / tariffs
    "tariff":           ["trade", "deal", "china", "billion", "economy", "tax"],
    "trade":            ["tariff", "deal", "china", "economy"],
    "china":            ["tariff", "trade", "deal", "fentanyl", "military"],
    "deal":             ["tariff", "trade", "negotiate", "billion"],

    # Immigration
    "border":           ["illegal alien", "wall", "immigration", "cartel", "fentanyl"],
    "illegal alien":    ["border", "wall", "deport", "immigration"],
    "wall":             ["border", "illegal alien", "immigration"],
    "deport":           ["illegal alien", "border", "wall"],

    # Energy
    "drill baby drill": ["energy", "oil", "gas", "fossil", "pipeline"],
    "energy":           ["drill baby drill", "oil", "gas", "pipeline"],
    "oil":              ["energy", "gas", "opec", "pipeline", "drill baby drill"],

    # Foreign policy — Iran
    "iran":             ["nuclear", "sanctions", "terrorist", "middle east"],
    "nuclear":          ["iran", "weapons", "sanctions"],
    "sanctions":        ["iran", "russia", "china"],

    # Foreign policy — Russia/Ukraine
    "russia":           ["ukraine", "nato", "putin", "war", "sanctions"],
    "ukraine":          ["russia", "nato", "war", "peace"],
    "nato":             ["russia", "europe", "military", "ukraine"],
    "putin":            ["russia", "ukraine", "peace", "deal"],

    # Economy
    "economy":          ["jobs", "billion", "tax", "tariff", "stock market"],
    "inflation":        ["economy", "biden", "prices", "border"],
    "stock market":     ["economy", "tariff", "billion", "jobs"],
    "jobs":             ["economy", "billion", "manufacturing"],

    # Trump's people / culture war
    "fake news":        ["media", "radical left", "cnn", "witch hunt"],
    "radical left":     ["democrat", "socialism", "fake news", "antifa"],
    "maga":             ["america first", "rally", "movement"],
    "america first":    ["maga", "tariff", "border", "energy"],

    # Biden/Democrats
    "biden":            ["sleepy joe", "democrat", "radical left", "autopen"],
    "sleepy joe":       ["biden", "autopen", "radical left"],
    "democrat":         ["radical left", "socialism", "biden", "pelosi"],

    # Rallies
    "rally":            ["maga", "america first", "crowd", "movement"],
    "crowd":            ["rally", "maga"],

    # Military / defense
    "military":         ["veteran", "border", "defense", "nato"],
    "veteran":          ["military", "soldier"],

    # Signing / policy actions
    "executive order":  ["signing", "policy", "regulation"],
    "signing":          ["executive order", "ceremony"],
}

# ─── Context type inference ───────────────────────────────────────────────────
# (pattern list, event_type, set of phrases that are common in that context type)
# If a post matches any pattern, all listed phrases get a small +CONTEXT_BOOST.
CONTEXT_PATTERNS: list[tuple[list[str], str, list[str]]] = [
    (
        ["rally", "massive crowd", "tonight at", "speaking at", "join me at",
         "see you at", "packed house", "thousands", "standing ovation"],
        "rally",
        ["maga", "america first", "radical left", "fake news", "rigged election",
         "sleepy joe", "drain the swamp", "build the wall", "lock her up"],
    ),
    (
        ["signing", "executive order", "just signed", "will sign", "signing ceremony",
         "landmark legislation"],
        "signing",
        ["executive order", "signing", "legislation", "regulation"],
    ),
    (
        ["press conference", "briefing", "reporters", "questions from the press",
         "speaking to the media"],
        "briefing",
        ["fake news", "radical left", "media"],
    ),
    (
        ["met with", "call with", "summit", "bilateral", "spoke with president",
         "diplomatic", "foreign minister"],
        "summit",
        ["deal", "peace", "negotiate", "nato", "agreement"],
    ),
    (
        ["visit to", "visiting", "factory", "plant", "facility", "company",
         "manufacturing", "roundtable"],
        "visit",
        ["jobs", "economy", "billion", "manufacturing", "tariff"],
    ),
    (
        ["address to congress", "joint session", "state of the union", "sotu"],
        "address",
        ["congress", "legislation", "economy", "border", "military"],
    ),
]

# ─── Pre/post event detection ─────────────────────────────────────────────────
# These patterns tell us if the post is BEFORE the event (boost signals) or
# AFTER (too late — dampen all boosts since the speech is over)
PRE_EVENT_PATTERNS = [
    r"tonight at \d",
    r"speaking (at|in|to)\b",
    r"join me (at|in)\b",
    r"(heading|flying|traveling) to\b",
    r"see you (at|in|tonight|soon)\b",
    r"(big|huge|massive) (rally|speech|event|crowd)",
    r"going to (say|talk|speak|address)\b",
    r"will be (speaking|talking|addressing)\b",
    r"coming (up|soon|tonight|today)\b",
    r"in \d+ (hours|minutes)\b",
    r"(about to|ready to) (speak|address|sign)",
]

POST_EVENT_PATTERNS = [
    r"just (finished|gave|delivered|signed|spoke|said|announced)",
    r"great (speech|rally|event|signing|meeting|call)\!",
    r"thank you .{0,30}(for coming|for being|rally|crowd)",
    r"(record|biggest|greatest|most watched) (crowd|audience|rally)",
    r"what a (night|day|crowd|rally|speech)\b",
    r"(loved|enjoyed) (every minute|talking|speaking|being)",
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Process Truth Social posts → signals.yaml")
    p.add_argument("--dry-run", action="store_true", help="Print changes, don't write")
    p.add_argument("--verbose", action="store_true", help="Show full post analysis")
    p.add_argument(
        "--max-age-days", type=float, default=7.0,
        help="Ignore posts older than this (default: 7 days)"
    )
    p.add_argument("--posts-path", type=Path, default=POSTS_PATH)
    return p.parse_args()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_posts(path: Path, max_age_days: float) -> list[dict]:
    if not path.exists():
        print(f"No Truth Social posts at {path} — run add_truth_social_post.py first")
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Error reading {path}: {exc}")
        return []

    posts = data.get("posts", [])
    if not posts:
        return []

    now = datetime.now(timezone.utc)
    recent = []
    for post in posts:
        posted_at = str(post.get("posted_at", "")).strip()
        if not posted_at:
            continue
        try:
            dt = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        age_hours = (now - dt).total_seconds() / 3600
        if age_hours <= max_age_days * 24:
            post["_age_hours"] = age_hours
            recent.append(post)

    print(f"Loaded {len(recent)} posts (within {max_age_days}d) from {len(posts)} total")
    return recent


def _load_trump_phrases() -> list[str]:
    if not MARKETS_PATH.exists():
        return []
    try:
        data = json.loads(MARKETS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    phrases: set[str] = set()
    for m in data.get("markets", []):
        speaker = str(m.get("speaker", "")).lower()
        if speaker not in ("trump", "auto", ""):
            continue
        for field in ("primary_phrase", "yes_sub_title"):
            p = str(m.get(field, "")).strip().lower()
            if p and len(p) >= 3:
                phrases.add(p)
        for v in m.get("phrase_variants", []):
            vv = str(v).strip().lower()
            if vv and len(vv) >= 3:
                phrases.add(vv)
    return sorted(phrases)


def _decay_mult(age_hours: float) -> float:
    for max_age, mult in DECAY_WINDOWS:
        if age_hours <= max_age:
            return mult
    return DEFAULT_MULT


def _extract_caps_words(text: str) -> set[str]:
    caps: set[str] = set()
    for m in re.findall(r'\b([A-Z]{3,}(?:\s+[A-Z]{3,}){1,3})\b', text):
        caps.add(m.lower())
        for w in m.lower().split():
            if len(w) >= 3:
                caps.add(w)
    for s in re.findall(r'\b([A-Z]{4,})\b', text):
        caps.add(s.lower())
    return caps


def _negated_phrases(text: str, phrases: list[str]) -> set[str]:
    """Find phrases that appear immediately after a negation word."""
    negation_pats = re.compile(
        r"\b(not|never|no|won't|wont|don't|dont|cannot|can't|cant|refuse|stop)\b"
        r"[\s\w,]{0,30}?",
        re.IGNORECASE,
    )
    negated: set[str] = set()
    text_lower = text.lower()
    for m in negation_pats.finditer(text_lower):
        window = text_lower[m.start(): m.start() + 80]
        for phrase in phrases:
            if re.search(r"\b" + re.escape(phrase) + r"\b", window):
                negated.add(phrase)
    return negated


def _infer_event_timing(text: str) -> str:
    """Return 'pre', 'post', or 'unknown' for whether this is before or after the event."""
    t = text.lower()
    for pat in POST_EVENT_PATTERNS:
        if re.search(pat, t):
            return "post"
    for pat in PRE_EVENT_PATTERNS:
        if re.search(pat, t):
            return "pre"
    return "unknown"


def _infer_context_type(text: str) -> tuple[str, list[str]]:
    """Return (event_type, implied_phrases) based on post content."""
    t = text.lower()
    for patterns, event_type, implied in CONTEXT_PATTERNS:
        if any(p in t for p in patterns):
            return event_type, implied
    return "general", []


def _analyze_post(
    post: dict,
    phrases: list[str],
    verbose: bool = False,
) -> dict[str, float]:
    """
    Analyze a single post and return phrase → multiplier mapping.
    Handles: time decay, ALL CAPS emphasis, negation, topic clusters,
    context type inference, and pre/post event detection.
    """
    content = str(post.get("content", ""))
    age_hours = float(post.get("_age_hours", 168.0))
    base_mult = _decay_mult(age_hours)
    content_lower = content.lower()
    caps_words = _extract_caps_words(content)

    # Detect event timing — if post-event, halve all boosts (speech already over)
    timing = _infer_event_timing(content)
    timing_scale = 0.5 if timing == "post" else 1.0

    # Detect context type → implied phrases
    context_type, context_implied = _infer_context_type(content)

    # Find negated phrases — these get damped instead of boosted
    negated = _negated_phrases(content, phrases)

    if verbose:
        age_str = f"{age_hours:.1f}h ago" if age_hours < 48 else f"{age_hours/24:.1f}d ago"
        print(f"\n{'─'*60}")
        print(f"Post ({age_str}, timing={timing}, context={context_type}):")
        print(f"  \"{content[:120]}{'...' if len(content)>120 else ''}\"")
        if negated:
            print(f"  Negated phrases: {sorted(negated)}")
        if context_implied:
            print(f"  Context-implied phrases: {context_implied[:6]}")

    scores: dict[str, float] = {}

    def _apply(phrase: str, mult: float, reason: str = "") -> None:
        """Apply a multiplier for a phrase, taking the max if already set."""
        if phrase in negated:
            # Negated: damp regardless of other signals
            scores[phrase] = min(scores.get(phrase, NEGATION_DAMP), NEGATION_DAMP)
            if verbose:
                print(f"    ✗ NEGATED \"{phrase}\" → {NEGATION_DAMP:.2f}x")
            return
        final = min(MAX_BOOST, mult * timing_scale)
        final = max(MIN_BOOST, final)
        if scores.get(phrase, 0.0) < final:
            scores[phrase] = final
            if verbose and final > 1.02:
                print(f"    → \"{phrase}\": {final:.2f}x  [{reason}]")

    # ── Direct phrase matches ─────────────────────────────────────────────────
    for phrase in phrases:
        if len(phrase) < 3:
            continue
        pat = r"\b" + re.escape(phrase) + r"\b"
        if not re.search(pat, content_lower):
            continue
        is_caps = any(w in caps_words for w in phrase.split() if len(w) >= 3)
        mult = base_mult + (CAPS_BONUS if is_caps else 0.0)
        caps_tag = " [CAPS]" if is_caps else ""
        _apply(phrase, mult, f"direct{caps_tag}")

        # ── Topic cluster expansion ───────────────────────────────────────────
        for implied in TOPIC_CLUSTERS.get(phrase, []):
            if implied in phrases:
                cluster_mult = base_mult * CLUSTER_SCALE + (CAPS_BONUS * 0.5 if is_caps else 0.0)
                _apply(implied, cluster_mult, f"cluster←{phrase}")

    # ── Context type boost ───────────────────────────────────────────────────
    # All phrases common in the inferred context type get a small flat boost
    for implied in context_implied:
        if implied in phrases or implied in {p for cluster in TOPIC_CLUSTERS.values() for p in cluster}:
            _apply(implied, MIN_BOOST + CONTEXT_BOOST, f"context:{context_type}")

    return scores


def _load_signals_map(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, dict] = {}
    for row in payload.get("signals", []):
        if not isinstance(row, dict):
            continue
        phrase = str(row.get("phrase", "")).strip().lower()
        if phrase:
            # Preserve ALL fields (including llm_boost, llm_reasoning, etc.) so
            # LLM-written values survive subsequent signal-fetch runs.
            node = dict(row)
            node["phrase"] = phrase
            node.setdefault("news_pressure", 1.0)
            node.setdefault("x_buzz", 1.0)
            out[phrase] = node
    return out


def _save_signals_map(path: Path, signal_map: dict[str, dict]) -> None:
    rows = []
    for p in sorted(signal_map.keys()):
        node = dict(signal_map[p])
        node["phrase"] = signal_map[p]["phrase"]
        node["news_pressure"] = float(node.get("news_pressure", 1.0))
        node["x_buzz"] = float(node.get("x_buzz", 1.0))
        node["updated_at"] = node.get("updated_at", "")
        rows.append(node)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"signals": rows}, sort_keys=False), encoding="utf-8")


def main() -> None:
    args = _parse_args()
    now_iso = _now_iso()

    posts = _load_posts(args.posts_path, args.max_age_days)
    if not posts:
        print("No recent posts — signals.yaml unchanged")
        return

    phrases = _load_trump_phrases()
    print(f"Loaded {len(phrases)} Trump phrase variants from Kalshi cache")
    if not phrases:
        print("No phrases — run fetch_markets.py first")
        return

    # Aggregate scores across all posts (max per phrase)
    all_scores: dict[str, float] = {}
    for post in posts:
        post_scores = _analyze_post(post, phrases, verbose=args.verbose)
        for phrase, mult in post_scores.items():
            if mult > all_scores.get(phrase, 0.0):
                all_scores[phrase] = mult

    # Merge into signals.yaml
    signal_map = _load_signals_map(SIGNALS_PATH)
    changed = 0
    boosted: list[tuple[str, float, float]] = []
    damped: list[tuple[str, float, float]] = []

    for phrase, ts_mult in all_scores.items():
        node = signal_map.get(phrase)
        if node is None:
            node = {"phrase": phrase, "news_pressure": 1.0, "x_buzz": 1.0, "updated_at": ""}
            signal_map[phrase] = node

        current = float(node.get("news_pressure", 1.0))

        if ts_mult <= NEGATION_DAMP:
            # Negated phrase — dampen
            new_val = round(min(current, NEGATION_DAMP), 3)
        else:
            # Boost — take the max (don't overwrite higher news signal)
            new_val = round(max(current, ts_mult), 3)

        if abs(new_val - current) >= 0.01:
            node["news_pressure"] = new_val
            node["updated_at"] = now_iso
            changed += 1
            if new_val > current:
                boosted.append((phrase, current, new_val))
            else:
                damped.append((phrase, current, new_val))

    if args.dry_run:
        print(f"\n[DRY RUN] Would update {changed} phrases in signals.yaml:")
        if boosted:
            print("  BOOSTED:")
            for phrase, old, new in sorted(boosted, key=lambda x: -x[2]):
                print(f"    \"{phrase}\": {old:.2f} → {new:.2f}x")
        if damped:
            print("  DAMPED (negated):")
            for phrase, old, new in sorted(damped, key=lambda x: x[2]):
                print(f"    \"{phrase}\": {old:.2f} → {new:.2f}x")
    else:
        if changed > 0:
            _save_signals_map(SIGNALS_PATH, signal_map)
            print(f"\nUpdated {changed} phrases in signals.yaml:")
            if boosted:
                for phrase, old, new in sorted(boosted, key=lambda x: -x[2])[:15]:
                    print(f"  ↑ \"{phrase}\": {old:.2f} → {new:.2f}x")
            if damped:
                for phrase, old, new in damped:
                    print(f"  ↓ \"{phrase}\" (NEGATED): {old:.2f} → {new:.2f}x")
        else:
            print("No changes needed — signals already up to date")


if __name__ == "__main__":
    main()
