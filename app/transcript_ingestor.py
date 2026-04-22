from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.phrase_matcher import PhraseMatcher, store_phrase_hits
from app.transcript_sources import TranscriptSource
from app.utils import append_jsonl, utc_now_iso

logger = logging.getLogger(__name__)


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class TranscriptIngestor:
    conn: sqlite3.Connection
    source: TranscriptSource
    matcher: PhraseMatcher
    transcript_urls: list[str]
    raw_log_path: Path
    _seen_hashes: dict[str, str] = field(default_factory=dict, repr=False)
    _prev_text: dict[str, str] = field(default_factory=dict, repr=False)

    def _is_duplicate(self, source_ref: str | None, text: str) -> bool:
        h = _text_hash(text)
        key = source_ref or "__no_ref__"

        if self._seen_hashes.get(key) == h:
            return True

        row = self.conn.execute(
            "SELECT 1 FROM transcripts WHERE source_ref = ? AND text_hash = ? LIMIT 1",
            (key, h),
        ).fetchone()
        if row:
            self._seen_hashes[key] = h
            return True

        self._seen_hashes[key] = h
        return False

    def _extract_new_text(self, source_ref: str | None, full_text: str) -> str:
        """Return only the portion of text that wasn't in the previous fetch.

        For growing transcript streams (live captions), we detect whether the
        new text starts with the old text and only return the delta.  If the
        text changed entirely (different page, or rewrite), we return the
        full text.
        """
        key = source_ref or "__no_ref__"
        prev = self._prev_text.get(key, "")
        self._prev_text[key] = full_text

        if prev and full_text.startswith(prev):
            delta = full_text[len(prev):]
            return delta.strip() if delta.strip() else ""
        return full_text

    def run_once(self) -> int:
        refs = self.transcript_urls or [None]
        inserted = 0

        for ref in refs:
            try:
                record = self.source.fetch(ref)
            except Exception as exc:
                logger.warning("Transcript fetch failed (%s): %s", ref, exc)
                continue

            if record is None:
                continue

            if self._is_duplicate(record.source_ref, record.text):
                logger.debug("Duplicate transcript skipped ref=%s", record.source_ref)
                continue

            new_text = self._extract_new_text(record.source_ref, record.text)

            ts = utc_now_iso()
            h = _text_hash(record.text)
            cursor = self.conn.execute(
                """
                INSERT INTO transcripts (ts, source, source_ref, text, text_hash)
                VALUES (?, ?, ?, ?, ?)
                """,
                (ts, record.source, record.source_ref, record.text, h),
            )
            transcript_id = int(cursor.lastrowid)

            match_text = new_text if new_text else record.text
            hits = self.matcher.find_hits(match_text)
            hit_count = store_phrase_hits(self.conn, transcript_id=transcript_id, hits=hits, ts=ts)

            raw_payload = {
                "ts": ts,
                "source": record.source,
                "source_ref": record.source_ref,
                "chars": len(record.text),
                "new_chars": len(new_text),
                "phrase_hits": hit_count,
                "text_hash": h,
            }
            append_jsonl(self.raw_log_path, raw_payload)
            logger.info(
                "Transcript ingested source=%s ref=%s chars=%s new=%s hits=%s",
                record.source,
                record.source_ref,
                len(record.text),
                len(new_text),
                hit_count,
            )
            inserted += 1

        if inserted == 0 and not self.transcript_urls:
            logger.info("No transcript URLs configured; ingestion loop idling cleanly.")

        self.conn.commit()
        return inserted

