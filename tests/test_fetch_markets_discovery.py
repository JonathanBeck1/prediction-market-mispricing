from __future__ import annotations

from scripts.fetch_markets import (
    _extract_event_ticker,
    _infer_speaker_for_market,
    _is_mention_like_event,
    _is_mention_like_market,
    _is_mention_like_series,
)


def test_mention_like_market_for_leavitt_briefing() -> None:
    market = {
        "title": "What will Karoline Leavitt say at her next press briefing?",
        "rules_primary": "",
    }
    assert _is_mention_like_market(market) is True


def test_non_mention_market_not_included() -> None:
    market = {
        "title": "Who will be Trump's next Press Secretary?",
        "rules_primary": "",
    }
    assert _is_mention_like_market(market) is False


def test_market_speaker_inference_prefers_leavitt() -> None:
    market = {
        "title": "Will the White House Press Secretary say Tariff at her next press briefing?",
        "rules_primary": "Karoline Leavitt is the moderator.",
    }
    assert _infer_speaker_for_market(market, fallback="trump") == "leavitt"


def test_series_without_say_or_mention_not_included() -> None:
    assert _is_mention_like_series("KXNEXTPRESSEC", "Who will be Trump's next Press Secretary?") is False


def test_events_endpoint_mention_like_detection() -> None:
    event_row = {
        "title": "What will Karoline Leavitt say in the next press briefing?",
        "description": "Mention market for next briefing.",
    }
    assert _is_mention_like_event(event_row) is True


def test_extract_event_ticker_from_market_style_ticker() -> None:
    event_row = {"ticker": "KXSECPRESSMENTION-26MAR10-TARIFF"}
    assert _extract_event_ticker(event_row) == "KXSECPRESSMENTION-26MAR10"
