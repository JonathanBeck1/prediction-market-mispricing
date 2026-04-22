"""Format action card payloads into human-readable text for WhatsApp/console."""
from __future__ import annotations

from typing import Any


def format_card_text(card: dict[str, Any]) -> str:
    """Format a single action card payload into a compact, phone-readable string.

    Designed for WhatsApp messages: no markdown, plain text, ~5-10 second read.
    """
    lines: list[str] = []

    side = card.get("side", "WATCH")
    market = card.get("market_id", "?")
    phrase = card.get("phrase", "?")
    speaker = card.get("subject", "?")

    # Header line with emoji-free side indicator
    side_label = {"BUY_YES": ">> BUY YES", "BUY_NO": ">> BUY NO", "WATCH": "-- WATCH"}.get(side, side)
    lines.append(f"{side_label} | {market}")
    lines.append(f"Speaker: {speaker} | Phrase: \"{phrase}\"")

    # Event context
    event = card.get("event", {})
    if event:
        state = event.get("speech_state", "")
        etype = event.get("event_type", "")
        event_line = f"Event: {etype}" if etype else "Event:"
        if state == "scheduled":
            starts_in = event.get("starts_in_sec")
            if starts_in:
                event_line += f" scheduled (starts in {_fmt_duration(starts_in)})"
            else:
                event_line += " scheduled"
        elif state == "live":
            remaining = card.get("time_remaining_sec")
            if remaining is not None:
                event_line += f" LIVE (~{_fmt_duration(remaining)} remaining)"
            else:
                event_line += " LIVE"
        elif state == "ended":
            event_line += " ENDED"
        elif state == "unknown":
            event_line += " (signal lost)"
        lines.append(event_line)
    else:
        lines.append("Event: none scheduled")

    # Scores
    scores = card.get("scores", {})
    p = scores.get("p_literal", card.get("p_literal", 0))
    base_rate = scores.get("base_rate")
    decay = scores.get("time_decay")
    news = scores.get("news_pressure", 1.0)
    buzz = scores.get("x_buzz", 1.0)

    score_parts = [f"p={p:.2f}"]
    if base_rate is not None:
        score_parts.append(f"base={base_rate:.2f}")
    if decay is not None and decay < 1.0:
        score_parts.append(f"decay={decay:.2f}")
    if news != 1.0:
        score_parts.append(f"news={news:.1f}")
    if buzz != 1.0:
        score_parts.append(f"buzz={buzz:.1f}")
    lines.append(" | ".join(score_parts))

    # Price and EV
    exec_hint = card.get("exec_price_hint", "")
    ev_yes = card.get("ev_yes", 0)
    ev_no = card.get("ev_no", 0)
    size_cap = card.get("size_cap", 0)

    if exec_hint:
        ev = ev_yes if side == "BUY_YES" else ev_no
        price_line = f"{exec_hint} | EV: {ev:+.2f}"
        if size_cap:
            price_line += f" | Cap: ${size_cap:.0f}"
        lines.append(price_line)

    # Reasons
    reasons = card.get("reason_codes", [])
    if reasons:
        lines.append(f"Reasons: {', '.join(reasons)}")

    return "\n".join(lines)


def format_card_oneliner(card: dict[str, Any]) -> str:
    """Ultra-compact one-line format for console logging."""
    side = card.get("side", "WATCH")
    market = card.get("market_id", "?")
    phrase = card.get("phrase", "?")
    p = card.get("p_literal", 0)
    ev_yes = card.get("ev_yes", 0)
    ev_no = card.get("ev_no", 0)
    hint = card.get("exec_price_hint", "")
    reasons = card.get("reason_codes", [])
    tag = f"[{side}]"
    parts = [
        tag,
        market,
        f'"{phrase}"',
        f"p={p:.3f}",
        f"ev_y={ev_yes:+.3f}",
        f"ev_n={ev_no:+.3f}",
    ]
    if hint:
        parts.append(hint)
    if reasons:
        parts.append(",".join(reasons[:3]))
    return " | ".join(parts)


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h = m // 60
    rm = m % 60
    if rm:
        return f"{h}h {rm}m"
    return f"{h}h"
