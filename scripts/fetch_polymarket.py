#!/usr/bin/env python3
"""Fetch Polymarket mention prices with confidence-scored Kalshi matching.

This script upgrades Poly integration from phrase-only matching to
speaker/timeframe-aware matching with quality filters.
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).parent.parent))

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
MENTIONS_PAGE = "https://polymarket.com/predictions/mention-markets"
OUTPUT_PATH = Path("data/polymarket_prices.json")
KALSHI_CACHE = Path("data/kalshi_markets.json")

MIN_QUALITY_SCORE = 0.35
BOOK_DEPTH_LEVELS = 5

SPEAKER_KEYWORDS = {
    "trump": ["trump"],
    "leavitt": ["leavitt", "press secretary", "secretary leavitt"],
    "mamdani": ["mamdani"],
    "sanders": ["bernie sanders", "bernie"],
    "kelly": ["mark kelly"],
    "conan": ["conan"],
    "starmer": ["starmer"],
    "fed": ["fomc", "federal reserve", "powell"],
    "nba": ["nba"],
    "survivor": ["survivor"],
    "jensen": ["jensen huang", "nvidia"],
}


def _fetch(url: str, timeout: float = 20, max_attempts: int = 6) -> str:
    req = Request(
        url,
        headers={
            "Accept": "application/json, text/html",
            "User-Agent": "kalshi-edge-poly/1.0",
        },
    )
    last: Exception | None = None
    for attempt in range(max_attempts):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        except HTTPError as e:
            last = e
            if e.code == 429 and attempt < max_attempts - 1:
                time.sleep(min(8.0, 0.5 * (attempt + 1)))
                continue
            raise
        except (URLError, OSError, TimeoutError) as e:
            last = e
            if attempt < max_attempts - 1:
                time.sleep(min(5.0, 0.4 * (attempt + 1)))
                continue
            raise
    if last:
        raise last
    return ""


def _fetch_json(url: str) -> list | dict:
    return json.loads(_fetch(url))


def _normalize_phrase(text: str) -> str:
    low = (text or "").lower().strip()
    low = re.sub(r"[“”\"']", "", low)
    low = re.sub(r"[^a-z0-9/\s-]", " ", low)
    low = re.sub(r"\s+", " ", low).strip()
    return low


def _phrase_alternatives(phrase: str) -> list[str]:
    p = _normalize_phrase(phrase)
    if not p:
        return []
    parts = [x.strip() for x in p.split("/") if x.strip()]
    out: list[str] = [p]
    out.extend(parts)
    return list(dict.fromkeys(out))


def _token_set(text: str) -> set[str]:
    return {t for t in _normalize_phrase(text).split() if len(t) >= 3}


def _timeframe_from_title(title: str) -> str:
    t = (title or "").lower()
    if "this week" in t or "before " in t:
        return "weekly"
    if any(m in t for m in ("in january", "in february", "in march", "in april",
                             "in may", "in june", "in july", "in august",
                             "in september", "in october", "in november", "in december")):
        return "monthly"
    if any(k in t for k in ("during ", "at ", "press conference", "remarks", "briefing")):
        return "event"
    return "unknown"


def _kalshi_timeframe(km: dict) -> str:
    series = str(km.get("series_ticker", "")).upper()
    title = str(km.get("title", ""))
    if "MONTH" in series:
        return "monthly"
    if any(k in series for k in ("NICKNAME", "DURATION", "LATE")):
        return "monthly"
    return _timeframe_from_title(title)


def discover_mention_slugs() -> list[str]:
    slugs: set[str] = set()
    print("Step 1: Scraping Polymarket mentions page for slugs...", flush=True)

    # 1. Scrape the Polymarket mentions page for slug patterns
    try:
        html = _fetch(MENTIONS_PAGE)
        slugs.update(re.findall(r'"slug":"(what-will-[^"]+)"', html))
        slugs.update(re.findall(r'"slug":"(what-will-be-said[^"]+)"', html))
        slugs.update(re.findall(r'"slug":"(will-\w+-say[^"]+)"', html))
        slugs.update(re.findall(r'"slug":"(mention-[^"]+)"', html))
        slugs.update(re.findall(r'"slug":"(\w+-mention[^"]*)"', html))
        # Also grab any slug containing a known speaker name
        for speaker_slug in ("trump", "leavitt", "mamdani", "sanders", "powell"):
            slugs.update(re.findall(rf'"slug":"([^"]*{speaker_slug}[^"]*)"', html))
        print(f"Discovered {len(slugs)} mention slugs from Polymarket page")
    except (HTTPError, URLError, OSError) as e:
        print(f"Warning: could not scrape mentions page: {e}")

    # 2. Query the Gamma API by tag — fetch enough pages to get all active mention events
    print("Step 2: Querying Gamma API for mention events...", flush=True)
    for offset in range(0, 400, 100):
        try:
            url = f"{GAMMA_API}/events?tag=mentions&limit=100&active=true&offset={offset}"
            data = _fetch_json(url)
            if not isinstance(data, list) or not data:
                break
            for ev in data:
                s = str(ev.get("slug", "")).strip()
                if s:
                    slugs.add(s)
            time.sleep(0.2)
        except Exception:
            break

    # 3. Targeted speaker searches using the Gamma search API
    print("Step 3: Running targeted speaker searches...", flush=True)
    search_queries = [
        "what will trump say", "trump mention", "trump weekly",
        "what will leavitt say", "leavitt mention", "leavitt briefing",
        "what will mamdani say", "mamdani mention",
        "what will bernie say", "what will powell say",
        "what will be said", "press briefing",
    ]
    seen_query_slugs: set[str] = set()
    for q in search_queries:
        try:
            url = f"{GAMMA_API}/events?tag=mentions&limit=50&active=true"
            # Also try the search/title endpoint
            search_url = (
                f"{GAMMA_API}/events?limit=50&active=true"
                f"&title={q.replace(' ', '%20')}"
            )
            for fetch_url in [url, search_url]:
                data = _fetch_json(fetch_url)
                if isinstance(data, list):
                    for ev in data:
                        s = str(ev.get("slug", "")).strip()
                        if s and s not in seen_query_slugs:
                            seen_query_slugs.add(s)
                            slugs.add(s)
            time.sleep(0.15)
        except Exception:
            pass

    # 4. Hardcoded fallback slugs for recurring weekly/monthly Polymarket series
    # These are the permanent slugs for the most important mention markets —
    # they don't change even when new weekly instances are created.
    fallback_slugs = [
        # Trump weekly say (most important source of same-week prices)
        "what-will-trump-say-this-week-march-9-march-15",
        "what-will-trump-say-this-week-march-15",
        "what-will-trump-say-this-week-march-16-march-22",
        "what-will-trump-say-this-week-march-22",
        "what-will-trump-say-this-week-march-23-march-29",
        "what-will-trump-say-this-week-march-29",
        "what-will-trump-say-in-march",
        "what-will-trump-post-this-week-march-9-march-15",
        # Kentucky / Thermo Fisher specific events
        "what-will-trump-say-during-kentucky-visit",
        "what-will-trump-say-during-trumprx-ohio-visit",
        "what-will-trump-say-during-womens-history-month-event",
        # Leavitt
        "what-will-leavitt-say-this-week",
        "what-will-leavitt-say-at-the-press-briefing",
        "leavitt-mention-march",
    ]
    for s in fallback_slugs:
        slugs.add(s)

    # Remove clearly irrelevant slugs (non-mention pages)
    irrelevant = {"predictions", "elections", "markets"}
    slugs -= irrelevant

    return sorted(slugs)


def _detect_speaker(title: str) -> str:
    t = (title or "").lower()
    for speaker, keywords in SPEAKER_KEYWORDS.items():
        if any(kw in t for kw in keywords):
            return speaker
    return "other"


def _extract_phrase(question: str) -> str:
    m = re.search(r'"(.+?)"', question or "")
    return m.group(1).strip() if m else ""


def _parse_prices(raw_prices: str) -> tuple[float, float]:
    try:
        prices = json.loads(raw_prices or "[]")
    except Exception:
        prices = []
    yes = float(prices[0]) if prices else 0.0
    no = float(prices[1]) if len(prices) > 1 else max(0.0, 1.0 - yes)
    return yes, no


_CLOB_UNAVAILABLE: bool = False  # set True on first 403 to stop wasting requests


def _fetch_book(token_id: str) -> dict | None:
    """Fetch orderbook depth from Polymarket CLOB for a given token."""
    global _CLOB_UNAVAILABLE
    if not token_id or _CLOB_UNAVAILABLE:
        return None
    url = f"{CLOB_API}/book?token_id={token_id}"
    try:
        data = _fetch_json(url)
        if not isinstance(data, dict):
            return None
        return data
    except HTTPError as e:
        if e.code == 403:
            _CLOB_UNAVAILABLE = True  # don't hammer a blocked endpoint
        return None
    except Exception:
        return None


def _parse_book_depth(book: dict | None) -> dict:
    """Extract bid/ask depth and spread from CLOB book response."""
    result = {"bid_depth": 0.0, "ask_depth": 0.0, "spread": 1.0, "best_bid": 0.0, "best_ask": 1.0}
    if not book:
        return result
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    if bids:
        for b in bids[:BOOK_DEPTH_LEVELS]:
            result["bid_depth"] += float(b.get("size", 0) or 0)
        result["best_bid"] = float(bids[0].get("price", 0) or 0)
    if asks:
        for a in asks[:BOOK_DEPTH_LEVELS]:
            result["ask_depth"] += float(a.get("size", 0) or 0)
        result["best_ask"] = float(asks[0].get("price", 1) or 1)
    if result["best_ask"] > result["best_bid"]:
        result["spread"] = round(result["best_ask"] - result["best_bid"], 4)
    else:
        result["spread"] = 0.0
    return result


def _quality_score(*, yes_price: float, volume: float, book_depth: dict | None = None) -> tuple[float, list[str]]:
    score = 1.0
    flags: list[str] = []
    if volume < 25:
        score *= 0.5
        flags.append("LOW_VOLUME")
    elif volume < 100:
        score *= 0.75
        flags.append("THIN_VOLUME")
    if yes_price <= 0.01 or yes_price >= 0.99:
        score *= 0.85
        flags.append("EXTREME_PRICE")
    if book_depth:
        total_depth = book_depth.get("bid_depth", 0) + book_depth.get("ask_depth", 0)
        spread = book_depth.get("spread", 1.0)
        if total_depth < 50:
            score *= 0.7
            flags.append("THIN_BOOK")
        elif total_depth > 500:
            score *= 1.1
            flags.append("DEEP_BOOK")
        if spread > 0.10:
            score *= 0.8
            flags.append("WIDE_SPREAD")
        elif spread < 0.03:
            score *= 1.05
            flags.append("TIGHT_SPREAD")
    return round(max(0.0, min(1.0, score)), 4), flags


def _load_kalshi_index() -> dict[str, list[dict]]:
    if not KALSHI_CACHE.exists():
        return {}
    data = json.loads(KALSHI_CACHE.read_text(encoding="utf-8"))
    by_speaker: dict[str, list[dict]] = defaultdict(list)
    for km in data.get("markets", []):
        speaker = str(km.get("speaker", "")).strip().lower()
        if not speaker:
            continue
        phrases = []
        primary = str(km.get("primary_phrase", "")).strip()
        if primary:
            phrases.append(primary)
        phrases.extend(str(v) for v in km.get("phrase_variants", []) if str(v).strip())
        for p in phrases:
            by_speaker[speaker].append({
                "ticker": km.get("ticker", ""),
                "event_ticker": km.get("event_ticker", ""),
                "speaker": speaker,
                "phrase_norm": _normalize_phrase(p),
                "timeframe": _kalshi_timeframe(km),
                "title": km.get("title", ""),
                "yes_ask_dollars": float(km.get("yes_ask_dollars", 0) or 0),
            })
    return dict(by_speaker)


def _match_score(
    *,
    poly_phrase: str,
    poly_timeframe: str,
    poly_event_title: str,
    candidate: dict,
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []

    if poly_phrase == candidate["phrase_norm"]:
        score += 0.65
        reasons.append("EXACT_PHRASE")
    else:
        poly_tokens = _token_set(poly_phrase)
        cand_tokens = _token_set(candidate["phrase_norm"])
        if poly_tokens and cand_tokens:
            inter = len(poly_tokens & cand_tokens)
            union = len(poly_tokens | cand_tokens)
            if union > 0:
                j = inter / union
                if j >= 0.7:
                    score += 0.50
                    reasons.append("TOKEN_OVERLAP_HIGH")
                elif j >= 0.4:
                    score += 0.30
                    reasons.append("TOKEN_OVERLAP_MED")
                elif j >= 0.2:
                    score += 0.15
                    reasons.append("TOKEN_OVERLAP_LOW")
            if poly_phrase in candidate["phrase_norm"] or candidate["phrase_norm"] in poly_phrase:
                score += 0.10
                reasons.append("SUBSTRING_MATCH")

    if poly_timeframe != "unknown" and poly_timeframe == candidate["timeframe"]:
        score += 0.15
        reasons.append("TIMEFRAME_MATCH")
    elif poly_timeframe != "unknown" and candidate["timeframe"] != "unknown" and poly_timeframe != candidate["timeframe"]:
        score -= 0.10
        reasons.append("TIMEFRAME_MISMATCH")

    poly_event_tokens = _token_set(poly_event_title)
    title_tokens = _token_set(candidate.get("title", ""))
    if poly_event_tokens and title_tokens:
        overlap = len(poly_event_tokens & title_tokens)
        if overlap >= 3:
            score += 0.15
            reasons.append("EVENT_CONTEXT_STRONG")
        elif overlap >= 2:
            score += 0.1
            reasons.append("EVENT_CONTEXT_OVERLAP")

    return round(max(0.0, min(1.0, score)), 4), reasons


def _confidence_bucket(conf: float) -> str:
    if conf >= 0.8:
        return "high"
    if conf >= 0.55:
        return "medium"
    return "low"


def _best_kalshi_match(
    *,
    speaker: str,
    poly_phrase: str,
    poly_timeframe: str,
    poly_event_title: str,
    kalshi_index: dict[str, list[dict]],
) -> dict | None:
    candidates = kalshi_index.get(speaker, [])
    if not candidates:
        return None

    best: dict | None = None
    for cand in candidates:
        score, reasons = _match_score(
            poly_phrase=poly_phrase,
            poly_timeframe=poly_timeframe,
            poly_event_title=poly_event_title,
            candidate=cand,
        )
        if best is None or score > best["score"]:
            best = {
                "score": score,
                "reasons": reasons,
                "ticker": cand["ticker"],
                "event_ticker": cand["event_ticker"],
                "yes_ask_dollars": cand.get("yes_ask_dollars", 0.0),
            }
    return best


def _parse_clob_token_ids(raw: str | list) -> list[str]:
    """Extract CLOB token IDs from either JSON string or list."""
    if isinstance(raw, list):
        return [str(t).strip() for t in raw if str(t).strip()]
    try:
        parsed = json.loads(raw or "[]")
        if isinstance(parsed, list):
            return [str(t).strip() for t in parsed if str(t).strip()]
    except Exception:
        pass
    return []


def fetch_event_markets(slug: str, kalshi_index: dict[str, list[dict]]) -> dict | None:
    url = f"{GAMMA_API}/events?slug={slug}"
    try:
        data = _fetch_json(url)
    except (HTTPError, URLError, OSError) as e:
        print(f"  Error fetching {slug}: {e}")
        return None
    if not data:
        return None

    event = data[0]
    event_title = str(event.get("title", ""))
    raw_markets = event.get("markets", [])
    open_markets = [m for m in raw_markets if not m.get("closed")]
    speaker = _detect_speaker(event_title)
    timeframe = _timeframe_from_title(event_title)

    markets: list[dict] = []
    for m in open_markets:
        question = str(m.get("question", ""))
        phrase_raw = _extract_phrase(question)
        phrase_norm = _normalize_phrase(phrase_raw)
        if not phrase_norm:
            continue

        yes_price, no_price = _parse_prices(str(m.get("outcomePrices", "[]")))
        volume = float(m.get("volumeNum", 0) or 0)

        token_ids = _parse_clob_token_ids(m.get("clobTokenIds", "[]"))
        book_data = None
        book_depth = None
        if token_ids:
            book_data = _fetch_book(token_ids[0])
            if book_data:
                book_depth = _parse_book_depth(book_data)
                time.sleep(0.05)

        quality, quality_flags = _quality_score(
            yes_price=yes_price, volume=volume, book_depth=book_depth
        )

        match = _best_kalshi_match(
            speaker=speaker,
            poly_phrase=phrase_norm,
            poly_timeframe=timeframe,
            poly_event_title=event_title,
            kalshi_index=kalshi_index,
        )
        match_score = float(match["score"]) if match else 0.0
        conf = round(0.70 * match_score + 0.30 * quality, 4)

        market_entry = {
            "poly_id": m.get("id", ""),
            "question": question,
            "phrase": phrase_raw.lower().strip(),
            "phrase_norm": phrase_norm,
            "phrase_alternatives": _phrase_alternatives(phrase_norm),
            "condition_id": str(m.get("conditionId", "")).strip().lower(),
            "clob_token_ids": m.get("clobTokenIds", "[]"),
            "yes_price": round(yes_price, 4),
            "no_price": round(no_price, 4),
            "volume": round(volume, 2),
            "slug": m.get("slug", ""),
            "closed": bool(m.get("closed", False)),
            "updated_at": m.get("updatedAt", ""),
            "quality_score": quality,
            "quality_flags": quality_flags,
            "match_score": match_score,
            "match_reasons": match.get("reasons", []) if match else [],
            "confidence_score": conf,
            "confidence_bucket": _confidence_bucket(conf),
            "is_usable": conf >= MIN_QUALITY_SCORE,
            "kalshi_ticker": match.get("ticker", "") if match else "",
            "kalshi_event_ticker": match.get("event_ticker", "") if match else "",
            "kalshi_yes": round(float(match.get("yes_ask_dollars", 0.0)), 4) if match else None,
        }
        if book_depth:
            market_entry["book_bid_depth"] = round(book_depth["bid_depth"], 2)
            market_entry["book_ask_depth"] = round(book_depth["ask_depth"], 2)
            market_entry["book_spread"] = round(book_depth["spread"], 4)
            market_entry["book_best_bid"] = round(book_depth["best_bid"], 4)
            market_entry["book_best_ask"] = round(book_depth["best_ask"], 4)

        markets.append(market_entry)

    return {
        "event_id": event.get("id", ""),
        "title": event_title,
        "slug": slug,
        "speaker": speaker,
        "timeframe": timeframe,
        "active": bool(event.get("active", False)),
        "closed": bool(event.get("closed", False)),
        "end_date": event.get("endDate", ""),
        "volume_24h": float(event.get("volume24hr", 0) or 0),
        "total_markets": len(raw_markets),
        "open_markets": len(open_markets),
        "usable_markets": sum(1 for x in markets if x.get("is_usable")),
        "markets": markets,
    }


def match_to_kalshi(poly_markets: list[dict]) -> list[dict]:
    matches = []
    for pm in poly_markets:
        kalshi_ticker = pm.get("kalshi_ticker", "")
        kalshi_yes = pm.get("kalshi_yes")
        if not kalshi_ticker or kalshi_yes is None:
            continue
        poly_yes = float(pm.get("yes_price", 0) or 0)
        diff = round(poly_yes - float(kalshi_yes), 4)
        entry = {
            "phrase": pm.get("phrase_norm", pm.get("phrase", "")),
            "poly_yes": poly_yes,
            "kalshi_yes": float(kalshi_yes),
            "diff": diff,
            "abs_diff": abs(diff),
            "poly_slug": pm.get("slug", ""),
            "kalshi_ticker": kalshi_ticker,
            "kalshi_speaker": pm.get("speaker", ""),
            "confidence_score": float(pm.get("confidence_score", 0) or 0),
            "confidence_bucket": pm.get("confidence_bucket", "low"),
            "poly_volume": float(pm.get("volume", 0) or 0),
            "book_spread": float(pm.get("book_spread", 0) or 0),
            "book_depth": round(
                float(pm.get("book_bid_depth", 0) or 0) + float(pm.get("book_ask_depth", 0) or 0), 2
            ),
        }
        matches.append(entry)
    matches.sort(key=lambda x: -x["abs_diff"])
    return matches


def main() -> None:
    print("Fetching Polymarket mention markets...", flush=True)
    kalshi_index = _load_kalshi_index()
    slugs = discover_mention_slugs()
    if not slugs:
        print("No mention slugs found.")
        return
    print(f"Found {len(slugs)} Polymarket mention slugs to fetch.", flush=True)

    all_events: list[dict] = []
    all_markets: list[dict] = []
    for i, slug in enumerate(slugs, 1):
        print(f"  [{i}/{len(slugs)}] Fetching {slug}...", flush=True)
        event = fetch_event_markets(slug, kalshi_index)
        if event and event["markets"]:
            all_events.append(event)
            for m in event["markets"]:
                m["speaker"] = event["speaker"]
                m["event_title"] = event["title"]
                m["timeframe"] = event["timeframe"]
                all_markets.append(m)
            print(
                f"    -> {event['open_markets']} open, usable={event['usable_markets']} "
                f"speaker={event['speaker']}",
                flush=True,
            )
        time.sleep(0.2)

    conf_counts = {"high": 0, "medium": 0, "low": 0}
    for m in all_markets:
        conf_counts[m.get("confidence_bucket", "low")] = conf_counts.get(
            m.get("confidence_bucket", "low"), 0
        ) + 1

    output = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "total_events": len(all_events),
        "total_markets": len(all_markets),
        "usable_markets": sum(1 for m in all_markets if m.get("is_usable")),
        "confidence_counts": conf_counts,
        "events": all_events,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, indent=2, ensure_ascii=True), encoding="utf-8")
    print(
        f"\nSaved {len(all_markets)} markets "
        f"({output['usable_markets']} usable) to {OUTPUT_PATH}",
        flush=True,
    )

    matches = match_to_kalshi(all_markets)
    if matches:
        print(f"\n=== CROSS-MARKET MATCHES ({len(matches)}) ===")
        print(
            f"{'Phrase':24s} {'Poly':>7s} {'Kalshi':>7s} {'Diff':>7s} "
            f"{'Conf':>5s}  Ticker"
        )
        print("-" * 100)
        for m in matches[:25]:
            flag = " ***" if m["abs_diff"] >= 0.10 else ""
            print(
                f"  {m['phrase'][:24]:24s} {m['poly_yes']:.3f}   {m['kalshi_yes']:.3f}   "
                f"{m['diff']:+.3f}  {m['confidence_bucket'][:5]:5s}  "
                f"{m['kalshi_ticker']}{flag}"
            )
    else:
        print("\nNo Kalshi-aligned Poly matches found.")


if __name__ == "__main__":
    main()
