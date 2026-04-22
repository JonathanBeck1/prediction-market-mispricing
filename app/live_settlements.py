"""Live same-event settlement signals + phrase co-occurrence boosts.

Two signal types:

1. SAME-EVENT SETTLEMENTS: When a phrase in the same Kalshi event settles YES/NO
   within the last 12 hours, that is a strong signal for unsettled markets.

2. CO-OCCURRENCE BOOSTS: From 7,626 historical outcomes, when phrase A settles YES,
   phrase B also tends to be YES with measurable frequency.  If "transgender"=YES
   today (as it did on 2026-03-11), then "autopen", "sleepy joe", and "fake news"
   are historically YES in the same event 100% of the time.

When a phrase in the same Kalshi event settles YES or NO within the last 12 hours,
that is a strong signal for unsettled markets in the same event:

  * A settled YES tells us the speech is active and that Trump/Leavitt is using
    that type of rhetoric.  Correlated phrases (known from historical co-occurrence)
    get a small boost.
  * A settled NO means the speaker is done and that phrase was not said.
    Correlated phrases get a slight discount.
  * The mere presence of ANY settlement in the event means the speech is live or
    recently ended → unsettled markets are approaching resolution.

Usage:
    settlements = LiveSettlements.from_cache()
    signal = settlements.get_signal(event_ticker="KXPRESMENTION-DJT26MAR12",
                                    phrase="drill",
                                    speaker="trump")
    if signal:
        # signal.event_is_active  — bool: speech is live/recently active
        # signal.phrase_settled_yes — bool: this exact phrase already settled YES
        # signal.phrase_settled_no  — bool: this exact phrase already settled NO
        # signal.p_adjustment       — float: suggested additive shift to p_literal
        # signal.yes_count          — int:  number of phrases that settled YES
        # signal.no_count           — int:  number of phrases that settled NO
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

SETTLEMENTS_CACHE = Path("data/live_settlements.json")
COOCCURRENCE_CACHE = Path("data/phrase_cooccurrence.json")

# Maximum age for considering a settlement "live"
_LIVE_WINDOW_HOURS = 6
# Small boost for being in an active event (any phrases settled recently)
_ACTIVE_EVENT_BOOST = 0.04
# Maximum p_literal boost from co-occurrence signal
_COOCCUR_MAX_BOOST = 0.15


@dataclass(frozen=True)
class CooccurrenceBoost:
    """Suggested p_literal boost from co-occurrence with already-settled phrases."""
    trigger_phrase: str      # the phrase that settled YES
    target_phrase: str       # the phrase being scored (would receive the boost)
    rate: float              # historical co-occurrence rate (0-1)
    n: int                   # number of events observed
    p_boost: float           # suggested additive shift (rate - context_prior, capped)


@dataclass(frozen=True)
class SettlementSignal:
    event_ticker: str
    phrase: str
    event_is_active: bool
    phrase_settled_yes: bool
    phrase_settled_no: bool
    yes_count: int
    no_count: int
    latest_settlement_age_minutes: float
    # Suggested p_literal adjustment — caller applies this additively
    p_adjustment: float
    # Co-occurrence boosts from settled phrases in the same event
    cooccurrence_boosts: tuple[CooccurrenceBoost, ...] = ()


@dataclass
class LiveSettlements:
    """Loads data/live_settlements.json + phrase co-occurrence index."""
    _events: dict[str, dict] = None  # type: ignore[assignment]
    _cooccur: dict[str, list[dict]] = None  # type: ignore[assignment]
    _fetched_at: str = ""
    _loaded: bool = False

    @classmethod
    def from_cache(
        cls,
        path: Path = SETTLEMENTS_CACHE,
        cooccur_path: Path = COOCCURRENCE_CACHE,
    ) -> "LiveSettlements":
        obj = cls(_events={}, _cooccur={})
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                obj._events = data.get("events", {})
                obj._fetched_at = data.get("fetched_at", "")
                obj._loaded = True
                logger.debug(
                    "Loaded live settlements: %d events from %s",
                    len(obj._events), obj._fetched_at[:19]
                )
            except Exception as exc:
                logger.warning("Could not load live settlements: %s", exc)
        if cooccur_path.exists():
            try:
                obj._cooccur = json.loads(cooccur_path.read_text(encoding="utf-8"))
                logger.debug(
                    "Loaded co-occurrence index: %d trigger phrases", len(obj._cooccur)
                )
            except Exception as exc:
                logger.warning("Could not load co-occurrence index: %s", exc)
        return obj

    def get_signal(
        self,
        event_ticker: str,
        phrase: str,
        speaker: str | None = None,
    ) -> SettlementSignal | None:
        """Return a signal for an unsettled market in the given event.

        Returns None if there is no recent settlement data for this event.
        Returns a SettlementSignal with p_adjustment=0 if the event is known
        but the phrase was not in the settled list.
        """
        if not event_ticker or not self._events:
            return None

        # Try exact event_ticker match first, then prefix match
        ev_data = self._events.get(event_ticker)
        if ev_data is None:
            # Try prefix — e.g. event_ticker "KXTRUMPSAY-26MAR16" might match
            # KXTRUMPSAY-26MAR16-WIND settlement data stored under KXTRUMPSAY-26MAR16
            for key in self._events:
                if event_ticker.startswith(key) or key.startswith(event_ticker):
                    ev_data = self._events[key]
                    break
        if ev_data is None:
            return None

        # Check freshness
        latest_str = ev_data.get("latest_settlement", "")
        age_minutes = float("inf")
        if latest_str:
            try:
                lt = datetime.strptime(latest_str[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
                lt = lt.replace(tzinfo=timezone.utc)
                age_minutes = (datetime.now(timezone.utc) - lt).total_seconds() / 60
            except ValueError:
                pass

        event_is_active = age_minutes <= (_LIVE_WINDOW_HOURS * 60)
        if not event_is_active:
            return None

        yes_phrases = {p.lower().strip() for p in ev_data.get("yes", [])}
        no_phrases = {p.lower().strip() for p in ev_data.get("no", [])}
        phrase_lower = phrase.lower().strip()

        # Also do a substring / word match for multi-word phrases
        def _phrase_in_set(p: str, phrase_set: set[str]) -> bool:
            if p in phrase_set:
                return True
            # e.g. "predict" matches "predict / prediction"
            for s in phrase_set:
                tokens_p = set(p.split())
                tokens_s = set(s.split("/"))
                tokens_s2 = {t.strip() for t in s.split()}
                if tokens_p & tokens_s or tokens_p & tokens_s2:
                    return True
            return False

        phrase_settled_yes = _phrase_in_set(phrase_lower, yes_phrases)
        phrase_settled_no = _phrase_in_set(phrase_lower, no_phrases)

        # Compute p_adjustment
        p_adj = 0.0
        if event_is_active and not phrase_settled_yes and not phrase_settled_no:
            # Event is active — small boost: speech is happening/happened, markets will resolve
            p_adj += _ACTIVE_EVENT_BOOST

        # Co-occurrence boosts: scan all settled YES phrases in this event.
        # For each settled phrase, check if it has a co-occurrence relationship
        # with the phrase being scored.
        cooccur_boosts: list[CooccurrenceBoost] = []
        if event_is_active and self._cooccur and not phrase_settled_yes:
            for settled_phrase in yes_phrases:
                predictors = self._cooccur.get(settled_phrase, [])
                for pred in predictors:
                    if _phrase_in_set(pred["phrase"], {phrase_lower}):
                        # This settled phrase predicts the current phrase at rate `pred["rate"]`
                        # Boost = (co-occur rate - 0.45 global default), capped at _COOCCUR_MAX_BOOST
                        rate = float(pred["rate"])
                        boost = min(_COOCCUR_MAX_BOOST, max(0.0, rate - 0.45))
                        cooccur_boosts.append(CooccurrenceBoost(
                            trigger_phrase=settled_phrase,
                            target_phrase=phrase_lower,
                            rate=rate,
                            n=int(pred["n"]),
                            p_boost=boost,
                        ))

        # Apply strongest co-occurrence boost (don't stack multiple — use max)
        if cooccur_boosts:
            best_boost = max(cooccur_boosts, key=lambda x: x.p_boost)
            p_adj += best_boost.p_boost

        # Note: we don't adjust for phrase_settled_yes/no here because those markets
        # should already be settled and not in the open market list.  If somehow they
        # appear, return a clear signal so the scorer can cap/floor p.

        return SettlementSignal(
            event_ticker=event_ticker,
            phrase=phrase_lower,
            event_is_active=event_is_active,
            phrase_settled_yes=phrase_settled_yes,
            phrase_settled_no=phrase_settled_no,
            yes_count=len(yes_phrases),
            no_count=len(no_phrases),
            latest_settlement_age_minutes=age_minutes,
            p_adjustment=p_adj,
            cooccurrence_boosts=tuple(cooccur_boosts),
        )
