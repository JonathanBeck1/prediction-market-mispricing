from __future__ import annotations

from scripts.fetch_polymarket import (
    _confidence_bucket,
    _match_score,
    _parse_book_depth,
    _quality_score,
)


def test_match_score_rewards_exact_phrase_and_timeframe() -> None:
    score, reasons = _match_score(
        poly_phrase="tariff",
        poly_timeframe="event",
        poly_event_title="Trump rally in Ohio",
        candidate={
            "phrase_norm": "tariff",
            "timeframe": "event",
            "title": "Will Trump say tariff during Ohio rally?",
        },
    )
    assert score >= 0.8
    assert "EXACT_PHRASE" in reasons
    assert "TIMEFRAME_MATCH" in reasons


def test_match_score_penalizes_timeframe_mismatch() -> None:
    score, reasons = _match_score(
        poly_phrase="tariff",
        poly_timeframe="monthly",
        poly_event_title="Trump monthly mentions",
        candidate={
            "phrase_norm": "tariff",
            "timeframe": "event",
            "title": "Will Trump say tariff?",
        },
    )
    assert "TIMEFRAME_MISMATCH" in reasons
    exact_score, _ = _match_score(
        poly_phrase="tariff",
        poly_timeframe="event",
        poly_event_title="Trump rally",
        candidate={"phrase_norm": "tariff", "timeframe": "event", "title": "Trump rally"},
    )
    assert score < exact_score


def test_match_score_detects_substring() -> None:
    score, reasons = _match_score(
        poly_phrase="tax cut",
        poly_timeframe="unknown",
        poly_event_title="Trump speech",
        candidate={
            "phrase_norm": "tax cut plan",
            "timeframe": "unknown",
            "title": "Trump speech",
        },
    )
    assert "SUBSTRING_MATCH" in reasons


def test_quality_score_penalizes_thin_and_extreme() -> None:
    score, flags = _quality_score(yes_price=0.995, volume=5.0)
    assert score < 0.6
    assert "LOW_VOLUME" in flags
    assert "EXTREME_PRICE" in flags


def test_quality_score_uses_book_depth() -> None:
    book = {"bid_depth": 1000, "ask_depth": 800, "spread": 0.02}
    score_deep, flags_deep = _quality_score(yes_price=0.50, volume=200, book_depth=book)
    assert "DEEP_BOOK" in flags_deep
    assert "TIGHT_SPREAD" in flags_deep

    thin_book = {"bid_depth": 10, "ask_depth": 5, "spread": 0.15}
    score_thin, flags_thin = _quality_score(yes_price=0.50, volume=200, book_depth=thin_book)
    assert "THIN_BOOK" in flags_thin
    assert "WIDE_SPREAD" in flags_thin
    assert score_deep > score_thin


def test_parse_book_depth_extracts_levels() -> None:
    book = {
        "bids": [
            {"price": "0.45", "size": "100"},
            {"price": "0.44", "size": "200"},
        ],
        "asks": [
            {"price": "0.48", "size": "80"},
            {"price": "0.49", "size": "120"},
        ],
    }
    depth = _parse_book_depth(book)
    assert depth["best_bid"] == 0.45
    assert depth["best_ask"] == 0.48
    assert depth["bid_depth"] == 300.0
    assert depth["ask_depth"] == 200.0
    assert abs(depth["spread"] - 0.03) < 0.001


def test_parse_book_depth_handles_none() -> None:
    depth = _parse_book_depth(None)
    assert depth["bid_depth"] == 0.0
    assert depth["ask_depth"] == 0.0
    assert depth["spread"] == 1.0


def test_confidence_bucket_thresholds() -> None:
    assert _confidence_bucket(0.81) == "high"
    assert _confidence_bucket(0.60) == "medium"
    assert _confidence_bucket(0.20) == "low"
