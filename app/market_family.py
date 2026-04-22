from __future__ import annotations

from datetime import datetime, timezone

WINDOWED_SERIES = {
    "KXTRUMPSAYMONTH",
    "KXTRUMPSAYNICKNAME",
}

# ── Events-per-calendar-day by series type ────────────────────────────────────
# Used to convert remaining calendar days → remaining event count N, so that
# the compound formula p = 1 - (1-p_per_event)^N uses the correct N instead
# of a time fraction.
#
# Rates are conservative medians — better to underestimate N (lower predicted
# probability) than overclaim certainty.
_EVENTS_PER_DAY: dict[str, float] = {
    # Monthly WH press briefings: Leavitt holds ~5 briefings/week Mon–Fri
    "KXSECPRESSMENTION": 5 / 7,
    "KXLEAVITTMENTION":  5 / 7,
    # Monthly Trump mention/say: Trump speaks 2–3 times per week
    "KXTRUMPSAYMONTH":   2.5 / 7,
    "KXTRUMPSAYNICKNAME": 2.5 / 7,
    "KXTRUMPMENTION":    2.5 / 7,
    "KXPRESMENTION":     2.5 / 7,
    # Weekly windows: Trump speaks/posts ~3 times per week across all channels.
    # These contracts resolve on ANY public utterance in a 7-day window — not
    # just scheduled speech events — so events_per_day is closer to 3/7.
    # Raised from 1/7 to 3/7 after live data (KXTRUMPSAY-26MAR23: 0% WR on
    # 23 BUY_NO bets) confirmed the model was severely underestimating compound
    # probability for common phrases across a full week.
    "KXTRUMPSAY":        3 / 7,
    "KXTRUMPSAYEP":      3 / 7,
    "KXMAMDANIMENTION":  1 / 7,
    # NBA: ~1.3 games per calendar day during regular season
    "KXNBAMENTION":      1.3,
    # Duration contracts / late-night windows
    "KXTRUMPLATE":       1 / 7,
    "KXLEAVITTLATE":     5 / 7,
}
_DEFAULT_EVENTS_PER_DAY = 1 / 7   # conservative fallback


def series_ticker_from_market_id(market_id: str) -> str:
    parts = market_id.split("-")
    return parts[0] if parts else ""


def market_family(series_ticker: str) -> str:
    series = (series_ticker or "").strip().upper()
    if not series:
        return "single_event"
    if series in WINDOWED_SERIES or "MONTH" in series or "NICKNAME" in series:
        return "windowed"
    return "single_event"


def uses_event_timing(series_ticker: str) -> bool:
    return market_family(series_ticker) == "single_event"


_MONTHLY_WINDOW_SERIES: frozenset[str] = frozenset({
    "KXTRUMPSAYMONTH",
    "KXTRUMPSAYNICKNAME",
    "KXSECPRESSMENTION",
    "KXLEAVITTMENTION",
    "KXMAMDANIMENTION",
    "KXTRUMPMENTION",
    "KXPRESMENTION",
    "KXNBAMENTION",
})

_WEEKLY_WINDOW_SERIES: frozenset[str] = frozenset({
    "KXTRUMPSAY",
    "KXTRUMPSAYEP",
    "KXTRUMPLATE",
    "KXLEAVITTLATE",
})


def default_window_days(series_ticker: str) -> float | None:
    series = (series_ticker or "").strip().upper()
    if series in _MONTHLY_WINDOW_SERIES or "MONTH" in series or "NICKNAME" in series:
        return 31.0
    if series in _WEEKLY_WINDOW_SERIES or "LATE" in series:
        return 7.0
    # Suffix-based fallback — e.g. new series with "MENTION" suffix
    if series.endswith("MENTION"):
        return 31.0
    return None


def events_per_day(series_ticker: str) -> float:
    """Return estimated events-per-calendar-day for a window series.

    Used to convert remaining_calendar_days → remaining_events_N so the
    compound formula 1-(1-p)^N uses event count, not time fraction.
    """
    series = (series_ticker or "").strip().upper()
    # Exact match first
    if series in _EVENTS_PER_DAY:
        return _EVENTS_PER_DAY[series]
    # Prefix / substring fallback
    for key, rate in _EVENTS_PER_DAY.items():
        if series.startswith(key) or key in series:
            return rate
    # Infer from series name
    if "MONTH" in series:
        return 2.5 / 7   # monthly → assume Trump-like cadence
    return _DEFAULT_EVENTS_PER_DAY


def remaining_events_in_window(
    close_time: str | None,
    *,
    series_ticker: str,
    now_dt: datetime,
) -> float | None:
    """Return estimated number of remaining EVENTS in the window.

    This replaces the time-fraction exponent in 1-(1-p)^N with a count that
    reflects the actual number of speeches/games remaining, not calendar days.

    Returns None if the window type or close_time is unknown.
    """
    window_days = default_window_days(series_ticker)
    if window_days is None or not close_time:
        return None

    try:
        close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
    except ValueError:
        return None
    if close_dt.tzinfo is None:
        close_dt = close_dt.replace(tzinfo=timezone.utc)

    remaining_cal_days = max(0.0, (close_dt - now_dt).total_seconds() / 86400.0)
    rate = events_per_day(series_ticker)
    n = remaining_cal_days * rate
    return max(0.0, n)


def remaining_window_fraction(
    close_time: str | None,
    *,
    series_ticker: str,
    now_dt: datetime,
) -> float | None:
    """Legacy time-fraction helper.  Prefer remaining_events_in_window()."""
    window_days = default_window_days(series_ticker)
    if window_days is None or not close_time:
        return None

    try:
        close_dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
    except ValueError:
        return None
    if close_dt.tzinfo is None:
        close_dt = close_dt.replace(tzinfo=timezone.utc)

    remaining_sec = max(0.0, (close_dt - now_dt).total_seconds())
    full_window_sec = window_days * 86400.0
    if full_window_sec <= 0:
        return None
    return min(1.0, remaining_sec / full_window_sec)
