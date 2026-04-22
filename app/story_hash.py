"""Canonical 16-hex story ids for signal provenance (fetch_signals + fetch_news_signals).

Aligned with headline token fingerprints so duplicate stories across pipelines
can match when text is similar."""
from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

STORY_HASH_MIN_TOKENS = 3

_STOP_WORDS: frozenset[str] = frozenset({
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "as", "is", "was", "are", "were", "be",
    "been", "has", "have", "had", "will", "would", "could", "should",
    "that", "this", "it", "its", "he", "she", "they", "we", "says",
    "said", "say", "over", "after", "amid", "about", "into", "not",
    "his", "her", "their", "also", "up", "new", "deal", "report",
})


def title_tokens_from_text(text: str) -> frozenset[str]:
    """Significant tokens (>=4 chars, not stopwords) for Jaccard / hashing."""
    t = (text or "").lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return frozenset(
        w for w in t.split()
        if len(w) >= 4 and w not in _STOP_WORDS
    )


def story_hash_from_tokens(tokens: frozenset[str]) -> str:
    blob = "|".join(sorted(tokens)).encode("utf-8", errors="replace")
    return hashlib.sha256(blob).hexdigest()[:16]


def story_hash_fallback(text: str) -> str:
    t = re.sub(r"\s+", " ", (text or "").lower().strip())[:500]
    return hashlib.sha256(t.encode("utf-8", errors="replace")).hexdigest()[:16]


def story_hash_for_news_item(item: dict) -> str:
    """Match fetch_signals `_dedup_news_items` fingerprint (title OR summary field)."""
    line = (item.get("title") or item.get("summary") or "").strip()
    tokens = title_tokens_from_text(line)
    if len(tokens) < STORY_HASH_MIN_TOKENS:
        return story_hash_fallback(line)
    return story_hash_from_tokens(tokens)


def story_hash_for_rss_entry(title: str, description: str = "") -> str:
    """RSS row with separate title + body (e.g. fetch_news_signals)."""
    combined = f"{title or ''} {description or ''}".strip()
    tokens = title_tokens_from_text(combined)
    if len(tokens) < STORY_HASH_MIN_TOKENS:
        return story_hash_fallback(combined)
    return story_hash_from_tokens(tokens)
