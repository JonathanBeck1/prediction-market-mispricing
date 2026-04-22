from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import NamedTuple


# ── Negation / attribution detection constants ────────────────────────────────

# Words that, appearing within _NEGATION_WINDOW tokens BEFORE a phrase match,
# indicate the phrase is used in a negative context ("will NOT say Iran").
_NEGATION_WORDS = frozenset([
    "not", "never", "without", "no", "won't", "wouldn't", "didn't",
    "doesn't", "don't", "cannot", "can't", "refuse", "refused",
    "avoiding", "avoid", "deny", "denies", "denied",
])

# Words / patterns suggesting the phrase is attributed to someone else rather
# than being spoken by the primary speaker.
_ATTRIBUTION_WORDS = frozenset([
    "said", "says", "claimed", "claims", "argued", "argues",
    "alleged", "alleges", "stated", "quoted", "according",
    "accused", "called",
])

# How many whitespace-separated tokens to scan before the phrase match
_NEGATION_WINDOW = 10


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PhraseHit:
    phrase:    str
    start_idx: int
    end_idx:   int
    snippet:   str
    negated:   bool = False    # True → phrase in negation context ("will NOT say X")
    attributed: bool = False   # True → phrase attributed to third party ("he said X")


class MatchContext(NamedTuple):
    negated:    bool
    attributed: bool
    pre_tokens: list[str]   # tokens before match (for debugging)


# ── Normalisation ─────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """NFC-normalize + replace Unicode punctuation variants with ASCII equivalents.

    ASR outputs and live captions frequently use smart quotes, en-dashes, and
    non-breaking spaces that won't match phrase patterns using straight ASCII.
    """
    text = unicodedata.normalize("NFC", text)
    # Smart single quotes → apostrophe
    text = text.replace("\u2018", "'").replace("\u2019", "'")
    # Smart double quotes → straight double quote
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    # En-dash / em-dash → hyphen
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    # Non-breaking space → regular space
    text = text.replace("\u00a0", " ")
    # Collapse runs of whitespace
    text = " ".join(text.split())
    return text


# ── Context analysis ──────────────────────────────────────────────────────────

def _analyse_context(text: str, match_start: int) -> MatchContext:
    """Examine the token window before a phrase match for negation / attribution."""
    pre_text   = text[max(0, match_start - 80): match_start]
    pre_tokens = pre_text.lower().split()[-_NEGATION_WINDOW:]

    negated    = bool(_NEGATION_WORDS & set(pre_tokens))
    attributed = bool(_ATTRIBUTION_WORDS & set(pre_tokens))

    # Also flag if match is inside quotation marks (attribution / rhetorical use)
    if not attributed:
        near_pre = text[max(0, match_start - 5): match_start]
        if '"' in near_pre or "'" in near_pre:
            attributed = True

    return MatchContext(negated=negated, attributed=attributed, pre_tokens=pre_tokens)


# ── Core matcher ─────────────────────────────────────────────────────────────

class PhraseMatcher:
    """Case-insensitive, Unicode-normalised phrase matcher with negation detection.

    Negation/attribution detection is advisory: `PhraseHit.negated` and
    `PhraseHit.attributed` are set when context suggests the phrase is not
    a genuine primary-speaker utterance.  Callers decide whether to suppress
    the signal; the hit is always recorded so analysts can audit.
    """

    def __init__(self, phrases: list[str]) -> None:
        self.phrases = phrases
        # Normalise phrases at compile time so runtime normalisation matches
        self._compiled: dict[str, tuple[str, re.Pattern[str]]] = {}
        for phrase in phrases:
            norm = _normalize(phrase)
            pat  = re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(norm)}(?![A-Za-z0-9])",
                flags=re.IGNORECASE,
            )
            self._compiled[phrase] = (norm, pat)

    @staticmethod
    def _snippet(text: str, start: int, end: int, window: int = 60) -> str:
        lo = max(0, start - window)
        hi = min(len(text), end + window)
        return text[lo:hi].replace("\n", " ").strip()

    @staticmethod
    def _speaker_label_spans(text: str) -> list[tuple[int, int]]:
        """Ranges for leading speaker labels like 'President Trump:'.

        We ignore matches inside these labels to avoid counting identity terms
        from transcript formatting rather than spoken content.
        """
        spans: list[tuple[int, int]] = []
        for m in re.finditer(r"(?m)^[A-Za-z][A-Za-z0-9 .'\-]{1,80}:\s*", text):
            spans.append((m.start(), m.end()))
        return spans

    @staticmethod
    def _in_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
        for lo, hi in spans:
            if lo <= pos < hi:
                return True
        return False

    def find_hits(self, text: str) -> list[PhraseHit]:
        """Find all phrase matches in *text*, with negation/attribution flags."""
        # Normalise once — all patterns are also normalised at compile time
        norm_text     = _normalize(text)
        hits: list[PhraseHit] = []
        ignored_spans = self._speaker_label_spans(norm_text)

        for phrase, (norm_phrase, pattern) in self._compiled.items():
            for match in pattern.finditer(norm_text):
                if self._in_spans(match.start(), ignored_spans):
                    continue

                ctx = _analyse_context(norm_text, match.start())
                hits.append(
                    PhraseHit(
                        phrase=phrase,
                        start_idx=match.start(),
                        end_idx=match.end(),
                        snippet=self._snippet(norm_text, match.start(), match.end()),
                        negated=ctx.negated,
                        attributed=ctx.attributed,
                    )
                )

        hits.sort(key=lambda h: (h.start_idx, h.end_idx))
        return hits

    def find_confirmed_hits(self, text: str) -> list[PhraseHit]:
        """Return only hits that are NOT negated and NOT attributed.

        Use this for live signal detection to avoid false positives.
        The full find_hits() result is still stored to DB for auditing.
        """
        return [h for h in self.find_hits(text) if not h.negated and not h.attributed]


# ── DB persistence ────────────────────────────────────────────────────────────

def store_phrase_hits(
    conn: sqlite3.Connection,
    transcript_id: int,
    hits: list[PhraseHit],
    ts: str | None = None,
) -> int:
    if not hits:
        return 0
    now      = ts or datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()
    hit_date = now[:10]
    conn.executemany(
        """
        INSERT INTO phrase_hits (ts, transcript_id, phrase, start_idx, end_idx, snippet, hit_date)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (now, transcript_id, hit.phrase, hit.start_idx, hit.end_idx, hit.snippet, hit_date)
            for hit in hits
        ],
    )
    conn.commit()
    return len(hits)
