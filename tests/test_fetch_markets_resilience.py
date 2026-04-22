from __future__ import annotations

from scripts.fetch_markets import (
    _merge_events_with_history,
    _should_preserve_previous_cache,
)


def test_preserve_previous_cache_on_large_drop_with_errors() -> None:
    keep = _should_preserve_previous_cache(
        new_count=80,
        previous_count=300,
        has_fetch_errors=True,
        min_ratio=0.6,
    )
    assert keep is True


def test_do_not_preserve_cache_without_fetch_errors() -> None:
    keep = _should_preserve_previous_cache(
        new_count=80,
        previous_count=300,
        has_fetch_errors=False,
        min_ratio=0.6,
    )
    assert keep is False


def test_merge_events_keeps_first_seen_and_updates_last_seen() -> None:
    previous = [
        {
            "event_ticker": "KXSECPRESSMENTION-26MAR29",
            "title": "What will Karoline Leavitt say in the next press briefing?",
            "first_seen_at": "2026-03-10T15:00:00+00:00",
            "last_seen_at": "2026-03-10T16:00:00+00:00",
        }
    ]
    merged = _merge_events_with_history(
        [
            {
                "event_ticker": "KXSECPRESSMENTION-26MAR29",
                "title": "What will Karoline Leavitt say in the next press briefing?",
            }
        ],
        previous,
    )
    assert len(merged) == 1
    assert merged[0]["first_seen_at"] == "2026-03-10T15:00:00+00:00"
    assert merged[0]["last_seen_at"]
