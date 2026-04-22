#!/usr/bin/env python3
"""D1 — Signal aggregation layer for the LLM reasoning pass.

Pulls from four sources and writes data/signal_context.json:

  1. White House RSS     — whitehouse.gov/news/feed/ (official agenda items)
  2. Google News RSS     — recent Trump/Leavitt headlines from AP/Reuters/NYT
  3. Truth Social posts  — data/truth_social_posts.json (populated externally)
  4. Google Trends       — realtime search volume spikes for political topics

The output is consumed by scripts/analyze_signals.py which sends it to
GPT-4o-mini for phrase-level probability adjustments.

Output: data/signal_context.json
  {
    "fetched_at": "...",
    "sources": {
      "whitehouse": [...],
      "google_news": [...],
      "truth_social": [...],
      "google_trends": [...]
    },
    "phrase_universe": [...]
  }
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import feedparser
import requests

from app.story_hash import STORY_HASH_MIN_TOKENS, story_hash_for_news_item, title_tokens_from_text
from app.db import connect as _db_connect

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
OUTPUT_PATH        = Path("data/signal_context.json")
TRUTH_SOCIAL_PATH  = Path("data/truth_social_posts.json")
MARKETS_PATH       = Path("data/kalshi_markets.json")
WH_SCHEDULE_PATH   = Path("data/wh_schedule.json")
CORPUS_ROOT        = Path("data/corpus")
DB_PATH            = Path("data/edge.db")
LLM_ANALYSIS_PATH  = Path("data/llm_analysis.json")

# ── Config ─────────────────────────────────────────────────────────────────────
NEWSAPI_KEY        = os.getenv("NEWSAPI_KEY", "")
MAX_ITEMS_PER_SRC  = 30
HTTP_TIMEOUT       = 12

_GOOGLE_NEWS_QUERIES = [
    "Trump speech remarks",
    "Trump executive order signing",
    "White House briefing Leavitt",
    "Trump Truth Social post",
    "Trump tariff China",
    "Trump Iran nuclear deal",
]

_TRENDS_KEYWORDS = [
    "Trump", "tariff", "executive order", "Iran", "Israel",
    "border wall", "china trade", "NATO", "DOGE", "Musk",
    "press briefing", "White House", "immigration",
]


# ──────────────────────────────────────────────────────────────────────────────
# Source 1: White House official RSS
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_whitehouse() -> list[dict]:
    url = "https://whitehouse.gov/news/feed/"
    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:MAX_ITEMS_PER_SRC]:
            items.append({
                "title":   entry.get("title", ""),
                "summary": entry.get("summary", "")[:300],
                "link":    entry.get("link", ""),
                "published": entry.get("published", ""),
                "tags": [t.get("term","") for t in entry.get("tags", [])],
            })
        logger.info("WH RSS: fetched %d items", len(items))
        return items
    except Exception as exc:
        logger.warning("WH RSS failed: %s", exc)
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Source 2: Google News RSS (free, no key)
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_google_news() -> list[dict]:
    items = []
    seen = set()
    for query in _GOOGLE_NEWS_QUERIES:
        if len(items) >= MAX_ITEMS_PER_SRC:
            break
        try:
            url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en-US&gl=US&ceid=US:en"
            feed = feedparser.parse(url)
            for entry in feed.entries[:8]:
                title = entry.get("title", "")
                if title in seen:
                    continue
                seen.add(title)
                items.append({
                    "title":     title,
                    "source":    entry.get("source", {}).get("title", ""),
                    "published": entry.get("published", ""),
                    "link":      entry.get("link", ""),
                    "query":     query,
                })
            time.sleep(0.4)  # be polite
        except Exception as exc:
            logger.warning("Google News query '%s' failed: %s", query, exc)
    logger.info("Google News: fetched %d items", len(items))
    return items[:MAX_ITEMS_PER_SRC]


# ──────────────────────────────────────────────────────────────────────────────
# Source 3: NewsAPI (optional — requires NEWSAPI_KEY env var)
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_newsapi() -> list[dict]:
    if not NEWSAPI_KEY:
        logger.info("NewsAPI: no NEWSAPI_KEY set, skipping")
        return []
    try:
        url = "https://newsapi.org/v2/everything"
        params = {
            "q": "Trump OR 'White House' OR Leavitt OR 'executive order'",
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": MAX_ITEMS_PER_SRC,
            "apiKey": NEWSAPI_KEY,
        }
        resp = requests.get(url, params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
        items = [{
            "title":     a.get("title", ""),
            "source":    a.get("source", {}).get("name", ""),
            "published": a.get("publishedAt", ""),
            "summary":   (a.get("description") or "")[:200],
        } for a in articles]
        logger.info("NewsAPI: fetched %d items", len(items))
        return items
    except Exception as exc:
        logger.warning("NewsAPI failed: %s", exc)
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Source 4: Google Trends (pytrends)
# ──────────────────────────────────────────────────────────────────────────────

def _fetch_google_trends() -> list[dict]:
    try:
        from pytrends.request import TrendReq
        pytrends = TrendReq(hl="en-US", tz=360, timeout=(5, 15))
        # Batch keywords in groups of 5 (pytrends limit)
        results = []
        for i in range(0, min(len(_TRENDS_KEYWORDS), 15), 5):
            batch = _TRENDS_KEYWORDS[i:i+5]
            try:
                pytrends.build_payload(batch, cat=0, timeframe="now 1-d", geo="US")
                df = pytrends.interest_over_time()
                if df.empty:
                    continue
                latest = df.iloc[-1]
                for kw in batch:
                    if kw in latest:
                        results.append({
                            "keyword": kw,
                            "interest": int(latest[kw]),
                            "timeframe": "last_24h",
                        })
                time.sleep(1.0)
            except Exception as exc:
                logger.debug("Trends batch %s failed: %s", batch, exc)
        # Sort by interest descending
        results.sort(key=lambda x: -x["interest"])
        logger.info("Google Trends: fetched %d keywords", len(results))
        return results
    except Exception as exc:
        logger.warning("Google Trends failed: %s", exc)
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Source 5: Truth Social posts (from external file)
# ──────────────────────────────────────────────────────────────────────────────

def _load_truth_social() -> list[dict]:
    if not TRUTH_SOCIAL_PATH.exists():
        logger.info("Truth Social: no posts file at %s", TRUTH_SOCIAL_PATH)
        return []
    try:
        from datetime import datetime, timezone as _tz
        raw = json.loads(TRUTH_SOCIAL_PATH.read_text(encoding="utf-8"))
        posts = raw.get("posts", []) if isinstance(raw, dict) else raw

        # Sort by posted_at (canonical schema field); fall back to created_at for
        # older entries that predate the schema rename.  Empty string sorts last.
        posts = sorted(
            posts,
            key=lambda p: p.get("posted_at", p.get("created_at", "")),
            reverse=True,
        )

        # Annotate each post with its age so the LLM context string can show it.
        # Still include posts up to 14 days old (background context for weekly/monthly
        # markets), but tag anything older than 7 days so the LLM down-weights it.
        _now = datetime.now(_tz.utc)
        tagged: list[dict] = []
        for p in posts:
            raw_ts = p.get("posted_at") or p.get("created_at") or ""
            age_h = None
            if raw_ts:
                try:
                    dt = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                    age_h = (_now - dt).total_seconds() / 3600
                except Exception:
                    pass
            if age_h is not None and age_h > 336:   # > 14 days — too stale even as background
                continue
            content = (p.get("content") or p.get("text") or p.get("body") or "").strip()
            post_id = str(p.get("id") or p.get("url") or "")
            p["source_story_hash"] = hashlib.sha256(
                f"{post_id}|{raw_ts}|{content[:400]}".encode("utf-8", errors="replace"),
            ).hexdigest()[:16]
            tagged.append(p)

        result = tagged[:MAX_ITEMS_PER_SRC]
        skipped = len(posts) - len(tagged)
        logger.info("Truth Social: loaded %d posts (%d skipped as >14d old)", len(result), skipped)
        return result
    except Exception as exc:
        logger.warning("Truth Social load failed: %s", exc)
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Source 6: Recent speech transcripts from data/corpus/
# ──────────────────────────────────────────────────────────────────────────────

def _load_recent_transcripts(max_files: int = 3, max_chars_each: int = 2000) -> list[dict]:
    """Return excerpts from the most recent corpus transcript files.

    The LLM uses these to see Trump's actual recent language patterns —
    far stronger signal than news headlines alone.  We scan per-speaker
    subdirs as well as the corpus root for flat .txt files.
    """
    if not CORPUS_ROOT.exists():
        return []

    # Collect all .txt files across subdirs
    all_files: list[Path] = []
    for path in CORPUS_ROOT.rglob("*.txt"):
        if path.name.startswith("_"):
            continue
        all_files.append(path)

    if not all_files:
        return []

    # Sort by modification time (most recent first)
    all_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    results: list[dict] = []
    for fp in all_files[:max_files]:
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
            # Extract meaningful excerpt: first max_chars_each chars after any header
            excerpt = text.strip()[:max_chars_each]
            # Derive speaker + event type from path or filename
            parts = fp.parts
            # corpus/speaker/filename or corpus/filename
            _approved_corpus = {"trump", "leavitt", "mamdani", "powell", "fed"}
            if len(parts) >= 3 and parts[-2] in _approved_corpus:
                speaker = parts[-2]
            elif len(parts) >= 3 and parts[-2] not in _approved_corpus:
                # Skip transcripts from removed speakers entirely
                continue
            else:
                speaker = "unknown"
            results.append({
                "file": fp.name,
                "speaker": speaker,
                "age_days": round((time.time() - fp.stat().st_mtime) / 86400, 1),
                "excerpt": excerpt,
            })
            logger.info("Transcript: loaded %s (%s, %.1fd old)", fp.name, speaker,
                        (time.time() - fp.stat().st_mtime) / 86400)
        except Exception as exc:
            logger.debug("Transcript load failed %s: %s", fp, exc)

    return results


# ──────────────────────────────────────────────────────────────────────────────
# Source 7: Per-phrase LLM signal history (for feedback loop)
# ──────────────────────────────────────────────────────────────────────────────

def _load_phrase_signal_history(top_n: int = 40) -> dict[str, dict]:
    """Return per-phrase LLM accuracy history from outcome_reviews.

    This gives the LLM concrete feedback on its own track record:
    which phrases it boosted/suppressed and whether those signals were correct.
    Limited to phrases where we have at least 3 LLM-influenced outcomes.
    """
    if not DB_PATH.exists():
        return {}
    try:
        import sqlite3
        conn = _db_connect(DB_PATH)
        rows = conn.execute("""
            SELECT phrase,
                   reason_codes,
                   outcome,
                   side,
                   realized_pnl
            FROM outcome_reviews
            WHERE reason_codes LIKE '%LLM%'
            ORDER BY prediction_ts DESC
            LIMIT 500
        """).fetchall()
        conn.close()
    except Exception as exc:
        logger.warning("phrase_signal_history DB read failed: %s", exc)
        return {}

    from collections import defaultdict
    stats: dict[str, dict] = defaultdict(lambda: {
        "boost_n": 0, "boost_wins": 0,
        "suppress_n": 0, "suppress_wins": 0,
    })

    for phrase, codes, outcome, side, pnl in rows:
        phrase_key = (phrase or "").lower().strip()
        if not phrase_key:
            continue
        won = (side == "BUY_YES" and outcome == "yes") or (side == "BUY_NO" and outcome == "no")
        if "LLM_BOOST" in codes:
            stats[phrase_key]["boost_n"] += 1
            if won:
                stats[phrase_key]["boost_wins"] += 1
        elif "LLM_SUPPRESS" in codes:
            stats[phrase_key]["suppress_n"] += 1
            if won:
                stats[phrase_key]["suppress_wins"] += 1

    # Filter to phrases with at least 2 LLM-influenced outcomes
    result = {}
    for phrase, s in stats.items():
        total = s["boost_n"] + s["suppress_n"]
        if total < 2:
            continue
        result[phrase] = {
            "boost_n": s["boost_n"],
            "boost_wr": round(s["boost_wins"] / s["boost_n"], 2) if s["boost_n"] else None,
            "suppress_n": s["suppress_n"],
            "suppress_wr": round(s["suppress_wins"] / s["suppress_n"], 2) if s["suppress_n"] else None,
        }

    # Return the most informative ones (most observations)
    sorted_phrases = sorted(result.items(), key=lambda x: -(x[1]["boost_n"] + x[1]["suppress_n"]))
    return dict(sorted_phrases[:top_n])


# ──────────────────────────────────────────────────────────────────────────────
# Source 8: Live Kalshi market prices (what the market thinks right now)
# ──────────────────────────────────────────────────────────────────────────────

def _load_kalshi_market_prices(max_markets: int = 80) -> list[dict]:
    """Return current Kalshi yes_ask prices for active phrase markets.

    This gives the LLM critical calibration: if a phrase is priced at 90¢,
    don't boost it further; if it's at 5¢, a big boost moves the needle.
    """
    if not MARKETS_PATH.exists():
        return []
    try:
        data = json.loads(MARKETS_PATH.read_text(encoding="utf-8"))
        prices = []
        for m in data.get("markets", []):
            if not _is_llm_relevant_market(m):
                continue
            phrase = (m.get("primary_phrase") or "").strip().lower()
            # field is yes_ask_dollars in kalshi_markets.json
            yes_ask = m.get("yes_ask_dollars") or m.get("yes_ask")
            if not phrase or yes_ask is None:
                continue
            try:
                yes_ask_f = float(yes_ask)
            except (ValueError, TypeError):
                continue
            if yes_ask_f <= 0 or yes_ask_f >= 1:
                continue  # skip settled/delisted markets
            if m.get("status") not in ("active", None, ""):
                continue  # skip non-active markets
            entry = {
                "phrase": phrase,
                "yes_ask": round(yes_ask_f, 3),
                "ticker": m.get("ticker", ""),
                "series": m.get("series_ticker", ""),
                "speaker": m.get("speaker", ""),
            }
            # Include open_interest and volume_24h if available for liquidity context
            if m.get("open_interest") is not None:
                entry["open_interest"] = m.get("open_interest")
            if m.get("volume_24h") is not None:
                entry["volume_24h"] = m.get("volume_24h")
            prices.append(entry)
        # Sort by open_interest desc so highest-liquidity markets are shown first
        prices.sort(key=lambda x: -float(x.get("open_interest") or 0))
        return prices[:max_markets]
    except Exception as exc:
        logger.warning("kalshi_market_prices load failed: %s", exc)
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Source 9: Polymarket prices (independent crowd calibration)
# ──────────────────────────────────────────────────────────────────────────────

POLY_PRICES_PATH = Path("data/polymarket_prices.json")


def _load_polymarket_prices(min_confidence: float = 0.50, max_items: int = 40) -> list[dict]:
    """Return Polymarket YES prices for phrases with sufficient confidence.

    These serve as an independent crowd calibration point for the LLM:
    'Polymarket is pricing "iran" at 61% for an event market — this crowd
    signal suggests a high probability regardless of news volume.'
    """
    if not POLY_PRICES_PATH.exists():
        return []
    try:
        data = json.loads(POLY_PRICES_PATH.read_text(encoding="utf-8"))
        items = []
        for event in data.get("events", []):
            speaker = event.get("speaker", "other")
            event_title = event.get("title", "")[:60]
            for m in event.get("markets", []):
                conf = float(m.get("confidence_score", 0) or 0)
                if conf < min_confidence:
                    continue
                phrase = (m.get("phrase_norm") or m.get("phrase", "")).lower().strip()
                if not phrase:
                    continue
                items.append({
                    "phrase": phrase,
                    "yes_price": round(float(m.get("yes_price", 0) or 0), 3),
                    "confidence": round(conf, 2),
                    "timeframe": m.get("timeframe", "unknown"),
                    "event": event_title,
                    "speaker": speaker,
                    "volume": round(float(m.get("volume", 0) or 0), 0),
                })
        # Sort by confidence then volume
        items.sort(key=lambda x: (-x["confidence"], -x["volume"]))
        return items[:max_items]
    except Exception as exc:
        logger.warning("polymarket_prices load failed: %s", exc)
        return []


# ──────────────────────────────────────────────────────────────────────────────
# Phrase universe: phrases from active Kalshi markets
# ──────────────────────────────────────────────────────────────────────────────

# Only these speakers benefit from LLM news-cycle analysis.
# Sports (nba/ncaab/mlb/mma) are game-commentary phrases — unaffected by news.
_LLM_SPEAKER_ALLOWLIST = {"trump", "leavitt", "mamdani", "powell", "fed"}
_LLM_SERIES_ALLOWLIST = {
    "KXTRUMPSAY", "KXTRUMPSAYEP", "KXTRUMPMENTION", "KXTRUMPMENTIONB",
    "KXDJTRALLY", "KXDJTINVESTMENT", "KXDJTWOMENS", "KXDJTCONF",
    "KXTRUMPSAYMONTH", "KXTRUMPSAYNICKNAME", "KXTRUMPLATE",
    "KXTRUMPMENTIONDURATION", "KXPRESMENTION",
    "KXLEAVITTMENTION", "KXLEAVITTSMFMENTION", "KXSECPRESSMENTION",
    "KXLEAVITTLATE", "KXLEAVITTMENTIONDURATION",
    "KXMAMDANIMENTION", "KXTRUMPSAYMAM",
    "KXFEDMENTION",
}


def _is_llm_relevant_market(m: dict) -> bool:
    """Return True only for markets the LLM can usefully analyze."""
    series = (m.get("series_ticker") or "").upper()
    speaker = (m.get("speaker") or "").lower()
    if series in _LLM_SERIES_ALLOWLIST:
        return True
    if speaker in _LLM_SPEAKER_ALLOWLIST:
        return True
    return False


def _load_phrase_universe() -> list[str]:
    if not MARKETS_PATH.exists():
        return []
    try:
        data = json.loads(MARKETS_PATH.read_text(encoding="utf-8"))
        phrases = set()
        for m in data.get("markets", []):
            if not _is_llm_relevant_market(m):
                continue
            p = m.get("primary_phrase") or ""
            if p:
                phrases.add(p.strip().lower())
        return sorted(phrases)
    except Exception:
        return []


# ──────────────────────────────────────────────────────────────────────────────
# News deduplication — entity-level token-Jaccard fingerprint
# ──────────────────────────────────────────────────────────────────────────────

_DEDUP_THRESHOLD = 0.55   # Jaccard ≥ 55% → treat as duplicate


def _title_tokens(item: dict) -> frozenset[str]:
    """Extract significant title tokens for deduplication fingerprinting."""
    line = (item.get("title") or item.get("summary") or "").strip()
    return title_tokens_from_text(line)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _dedup_news_items(
    items: list[dict],
    threshold: float = _DEDUP_THRESHOLD,
) -> list[dict]:
    """Remove near-duplicate news items using token-Jaccard similarity.

    Keeps the first occurrence (assumed higher-authority source since callers
    pass items in priority order: WH official → NewsAPI → Google News).
    Attaches `dedup_cluster_size` to kept items so the scorer can see how many
    outlets covered the same story.
    """
    kept: list[dict] = []
    kept_tokens: list[frozenset[str]] = []

    for item in items:
        tokens = _title_tokens(item)
        if len(tokens) < STORY_HASH_MIN_TOKENS:
            item["source_story_hash"] = story_hash_for_news_item(item)
            kept.append(item)
            kept_tokens.append(tokens)
            continue

        duplicate_of: int | None = None
        for idx, ktok in enumerate(kept_tokens):
            if _jaccard(tokens, ktok) >= threshold:
                duplicate_of = idx
                break

        if duplicate_of is not None:
            # Increment cluster size on the kept representative
            rep = kept[duplicate_of]
            rep["dedup_cluster_size"] = rep.get("dedup_cluster_size", 1) + 1
        else:
            item["dedup_cluster_size"] = 1
            item["source_story_hash"] = story_hash_for_news_item(item)
            kept.append(item)
            kept_tokens.append(tokens)

    return kept


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main() -> None:
    logger.info("=== fetch_signals: starting signal aggregation ===")

    wh_items        = _fetch_whitehouse()
    gnews_items     = _fetch_google_news()
    newsapi_items   = _fetch_newsapi()
    ts_posts        = _load_truth_social()
    trends          = _fetch_google_trends()
    transcripts     = _load_recent_transcripts()
    phrase_history  = _load_phrase_signal_history()
    phrases         = _load_phrase_universe()
    kalshi_prices   = _load_kalshi_market_prices()
    poly_prices     = _load_polymarket_prices()

    # ── Deduplicate news items across sources ─────────────────────────────────
    # Merge all news in authority order (WH official > NewsAPI > Google) then
    # remove near-duplicates so the LLM doesn't amplify stories covered by many
    # outlets as if they were independent signals.
    all_news_raw = wh_items + newsapi_items + gnews_items
    all_news_deduped = _dedup_news_items(all_news_raw)
    _dedup_removed = len(all_news_raw) - len(all_news_deduped)
    logger.info(
        "News dedup: %d raw → %d unique items (%d duplicates removed)",
        len(all_news_raw), len(all_news_deduped), _dedup_removed,
    )
    # Re-split by source tag for backward-compat context keys (items keep their origin)
    wh_items_deduped     = [i for i in all_news_deduped if i in wh_items]
    newsapi_deduped      = [i for i in all_news_deduped if i in newsapi_items]
    gnews_deduped        = [i for i in all_news_deduped if i in gnews_items]

    # Merge WH official items with any existing wh_schedule.json events
    wh_scheduled: list[dict] = []
    if WH_SCHEDULE_PATH.exists():
        try:
            wh_data = json.loads(WH_SCHEDULE_PATH.read_text(encoding="utf-8"))
            wh_scheduled = wh_data.get("events", [])[:20]
        except Exception:
            pass

    context: dict[str, Any] = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "whitehouse_rss":      wh_items_deduped,
            "whitehouse_schedule": wh_scheduled,
            "google_news":         gnews_deduped,
            "newsapi":             newsapi_deduped,
            "truth_social":        ts_posts,
            "google_trends":       trends,
            "recent_transcripts":  transcripts,
            "kalshi_market_prices": kalshi_prices,
            "polymarket_prices":   poly_prices,
        },
        "phrase_universe": phrases,
        "phrase_signal_history": phrase_history,
        "stats": {
            "wh_rss":            len(wh_items_deduped),
            "wh_scheduled":      len(wh_scheduled),
            "google_news":       len(gnews_deduped),
            "newsapi":           len(newsapi_deduped),
            "truth_social":      len(ts_posts),
            "google_trends":     len(trends),
            "transcripts":       len(transcripts),
            "phrase_history":    len(phrase_history),
            "total_phrases":     len(phrases),
            "kalshi_prices":     len(kalshi_prices),
            "poly_prices":       len(poly_prices),
            "news_dedup_removed": _dedup_removed,
        },
    }

    OUTPUT_PATH.write_text(json.dumps(context, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(
        "Wrote %s  (wh=%d gn=%d ts=%d trends=%d transcripts=%d phrases=%d history=%d kalshi=%d poly=%d)",
        OUTPUT_PATH,
        len(wh_items), len(gnews_items), len(ts_posts), len(trends),
        len(transcripts), len(phrases), len(phrase_history),
        len(kalshi_prices), len(poly_prices),
    )


if __name__ == "__main__":
    main()
