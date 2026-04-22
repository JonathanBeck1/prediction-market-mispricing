"""Bayesian Scorer — mispricing detector using Beta-Binomial posteriors.

This replaces the 2,500-line ScoringEngine with a fundamentally different approach:

Instead of:  base_rate × signals × boosts → p_literal → Platt → EV → 20 gates
We do:       observed outcomes → Beta posterior → CI excludes market price? → bet

The core insight from live P&L data:
  - The old model's Brier score (0.352) was WORSE than using market price (0.238)
  - Every signal (LLM, news, Poly) degraded performance when added to base rates
  - The only profitable segments exploited structural mispricing, not prediction

This scorer detects mispricing by:
  1. Computing a Bayesian posterior for each (speaker, phrase) YES rate
  2. Using speaker-level hierarchical priors for thin-data phrases
  3. Betting only when the 90% credible interval EXCLUDES the market price
  4. Sizing via Kelly criterion on the posterior mean

No LLM signals. No news pressure. No Platt scaling. No calibration floors.
Just observed frequencies, honest uncertainty, and market price comparison.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.bayesian_rates import BayesianRateStore, BayesianRate
from app.card_formatter import format_card_oneliner
from app.event_detector import EventDetector, EventInfo
from app.market_family import (
    market_family,
    series_ticker_from_market_id,
)
from app.notifier import WhatsAppNotifier
from app.utils import append_jsonl, utc_now_iso

logger = logging.getLogger(__name__)

# Minimum Kelly fraction to accept a bet — filters thin-edge noise
KELLY_MIN = 0.05

# Minimum observations for a (speaker, phrase) to be tradeable.
# Below this, the CI is too wide to exclude any reasonable market price.
MIN_OBS = 3

# Minimum confidence (1 - CI width) to trade.  Phrases where we have
# many observations produce tight CIs and high confidence.
MIN_CONFIDENCE = 0.50

# Maximum position size tiers based on posterior confidence
_SIZE_TIERS: list[tuple[float, float]] = [
    (0.80, 20.0),   # very tight CI → full size
    (0.65, 10.0),   # moderate CI → half size
    (0.50,  5.0),   # wide CI → minimum size
]

# Removed series — markets we've decided not to trade for structural reasons
_REMOVED_SERIES: frozenset[str] = frozenset({
    "KXHOCHULMENTION", "KXNEWSOMMENTION", "KXSTARMERMENTIONB",
    "KXFOXNEWSMENTION", "KXLASTWORDMENTION", "KXLASTWORDCOUNT",
    "KXSNLMENTION", "KXMRBEASTMENTION", "KXWHPRESSBRIEFING",
    "KXCARNEYMENTION", "KXHOMANMENTION", "KXMENTION",
    "KXPERSONMENTION", "KXPOLITICSMENTION", "KXNYCMDEBMENTION",
    "KXMENTIONEARNDAL", "KXSURVIVORMENTION", "KXWBCMENTION",
    "KXENTMENTION", "KXEARNINGSMENTIONULTA", "KXEARNINGSMENTIONEA",
    "KXEARNINGSMENTIONADBE", "KXJENSENMENTION",
    "KXMTPMENTION", "KXFTNMENTION", "KXMADDOWMENTION",
    "KXPELOSIMENTION", "KXINFANTINOMENTION", "KXDIMONMENTION",
    "KXBARRMENTION", "KXSCOTUSMENTION",
    "KXLEAVITTMENTIONDURATION", "KXTRUMPMENTIONDURATION",
})

_REMOVED_SPEAKERS: frozenset[str] = frozenset({
    "hochul", "newsom", "starmer", "snl", "mrbeast", "whitehouse",
    "carney", "homan", "auto", "survivor", "wbc",
    "entertainment", "ulta", "ea", "adobe", "jensen",
})

# Max BUY cards per event (correlated bets protection)
MAX_BUY_PER_EVENT = 10
MAX_RISK_PER_EVENT = 150.0


def _kelly_fraction(ev: float, loss_if_wrong: float) -> float:
    """Kelly f = edge / odds.  Returns 0 if non-positive."""
    if loss_if_wrong <= 0:
        return 0.0
    return max(0.0, ev / loss_if_wrong)


@dataclass
class BayesianScorer:
    """Mispricing detector using hierarchical Beta-Binomial posteriors."""

    conn: sqlite3.Connection
    action_cards_path: Path
    event_detector: EventDetector
    bayesian_rates: BayesianRateStore
    market_phrases: dict[str, list[str]] = field(default_factory=dict, repr=False)
    notifier: WhatsAppNotifier | None = None
    focus_event_markets: bool = False
    max_spread: float = 0.15
    min_depth: float = 50.0

    # Internal state
    _prior_cards: dict[str, dict] = field(default_factory=dict, repr=False)
    _last_recorded_side: dict[str, str] = field(default_factory=dict, repr=False)
    _cooldown_sec: float = 120.0
    _material_ev_delta: float = 0.03

    def run_once(self) -> int:
        """Score all markets. Returns count of markets scored."""
        now = utc_now_iso()
        now_dt = datetime.now(tz=timezone.utc)
        today = now[:10]

        rows = self.conn.execute(
            "SELECT market_id, subject FROM markets ORDER BY market_id"
        ).fetchall()
        snapshot_map = {
            row["market_id"]: row for row in self._latest_snapshots()
        }

        self.event_detector.run_transitions()

        pending: list[tuple[str, float, dict, bool]] = []
        count = 0

        for market in rows:
            market_id = str(market["market_id"])
            speaker = str(market["subject"]).lower().strip()
            series_ticker = series_ticker_from_market_id(market_id)
            market_family_name = market_family(market_id)

            # Skip removed markets/speakers
            _market_series = market_id.split("-")[0].upper() if market_id else ""
            if _market_series in _REMOVED_SERIES or speaker in _REMOVED_SPEAKERS:
                continue

            snap = snapshot_map.get(market_id)
            if snap is None:
                continue

            # Get phrase for this market
            hit_today, phrase = self._market_hit_today(market_id, today)
            if phrase is None:
                phrase = ""
            known_phrases = [p for p in self.market_phrases.get(market_id, []) if p]
            if not phrase and known_phrases:
                phrase = known_phrases[0]
            if not known_phrases and not phrase:
                continue

            # Market data
            yes_ask = float(snap["yes_ask"]) if snap["yes_ask"] is not None else 0.5
            no_ask = float(snap["no_ask"]) if snap["no_ask"] is not None else 0.5
            yes_bid = float(snap["yes_bid"]) if snap["yes_bid"] is not None else 0.0
            depth_yes = float(snap["depth_yes"]) if snap["depth_yes"] is not None else 0.0
            depth_no = float(snap["depth_no"]) if snap["depth_no"] is not None else 0.0
            yes_spread = round(yes_ask - yes_bid, 4) if yes_ask > yes_bid else 0.0

            reason_codes: list[str] = ["BAYESIAN_V1"]
            components: dict = {}

            # --- Core: get Bayesian posterior ---
            rate = self.bayesian_rates.get(speaker, phrase)
            if rate is None:
                # No data for this (speaker, phrase) — use speaker prior
                speaker_prior = self.bayesian_rates.get_speaker_prior(speaker)
                components["source"] = "speaker_prior"
                components["speaker_prior"] = round(speaker_prior, 4)
                reason_codes.append("NO_PHRASE_DATA")
                # Can't trade without phrase-level data — CI is undefined
                side = "WATCH"
                p_model = speaker_prior
                ci_low = 0.0
                ci_high = 1.0
                confidence = 0.0
            else:
                p_model = rate.mean
                ci_low = rate.ci_low
                ci_high = rate.ci_high
                confidence = rate.confidence
                components["source"] = "bayesian_posterior"
                components["alpha"] = rate.alpha
                components["beta"] = rate.beta
                components["n_obs"] = rate.n_obs
                components["posterior_mean"] = round(p_model, 4)
                components["ci_low"] = round(ci_low, 4)
                components["ci_high"] = round(ci_high, 4)
                components["confidence"] = round(confidence, 3)
                components["speaker_prior"] = round(
                    self.bayesian_rates.get_speaker_prior(speaker), 4
                )

                if rate.n_obs >= 10:
                    reason_codes.append("THICK_DATA")
                elif rate.n_obs >= MIN_OBS:
                    reason_codes.append("THIN_DATA")

                # --- Decision logic: does CI exclude market price? ---
                side = "WATCH"

                if confidence < MIN_CONFIDENCE:
                    reason_codes.append("LOW_CONFIDENCE")
                elif yes_ask < ci_low:
                    # Market underprices YES — our CI says true rate is higher
                    side = "BUY_YES"
                    reason_codes.append("CI_EXCLUDES_MARKET_YES")
                elif (1.0 - no_ask) > ci_high:
                    # Market implied YES > our CI upper bound → overpriced YES → BUY_NO
                    # (1 - no_ask) is the market's implied YES probability from the NO side
                    market_implied_yes = 1.0 - no_ask
                    if market_implied_yes > ci_high:
                        side = "BUY_NO"
                        reason_codes.append("CI_EXCLUDES_MARKET_NO")
                elif no_ask < ci_low:
                    # no_ask < ci_low means market thinks NO is cheap (YES is expensive)
                    # but our model says YES rate is at least ci_low
                    # Actually: if yes_ask > ci_high, market overprices YES → BUY_NO
                    if yes_ask > ci_high:
                        side = "BUY_NO"
                        reason_codes.append("CI_EXCLUDES_MARKET_NO")

            # Compute EV from posterior mean
            ev_yes = round(p_model - yes_ask, 4)
            ev_no = round((1.0 - p_model) - no_ask, 4)
            components["ev_yes"] = ev_yes
            components["ev_no"] = ev_no

            # --- Structural gates (minimal, evidence-based) ---

            # Settled market: don't fight near-resolved prices
            if side == "BUY_NO" and yes_ask >= 0.85:
                reason_codes.append("SETTLED_MARKET_BLOCK")
                side = "WATCH"
            elif side == "BUY_YES" and no_ask >= 0.85:
                reason_codes.append("SETTLED_MARKET_BLOCK")
                side = "WATCH"

            # Cheap NO block: when no_ask < 0.40, market thinks YES is very likely
            if side == "BUY_NO" and no_ask < 0.40:
                reason_codes.append("CHEAP_NO_BLOCK")
                side = "WATCH"

            # Expensive NO: paying >55c for NO has terrible risk/reward
            if side == "BUY_NO" and no_ask > 0.55:
                reason_codes.append("EXPENSIVE_NO_BLOCK")
                side = "WATCH"

            # Kelly gate: reject thin-edge bets
            if side == "BUY_YES":
                kf = _kelly_fraction(ev_yes, 1.0 - yes_ask)
                components["kelly_fraction"] = round(kf, 4)
                if kf < KELLY_MIN:
                    reason_codes.append("KELLY_WEAK")
                    side = "WATCH"
            elif side == "BUY_NO":
                kf = _kelly_fraction(ev_no, 1.0 - no_ask)
                components["kelly_fraction"] = round(kf, 4)
                if kf < KELLY_MIN:
                    reason_codes.append("KELLY_WEAK")
                    side = "WATCH"

            # Spread gate for YES (liquidity proxy)
            if side == "BUY_YES" and yes_spread > self.max_spread:
                reason_codes.append("SPREAD_TOO_WIDE")
                side = "WATCH"

            # Depth gate
            depth_for_side = depth_yes if side == "BUY_YES" else depth_no
            if side in ("BUY_YES", "BUY_NO") and depth_for_side < self.min_depth:
                reason_codes.append("DEPTH_TOO_THIN")
                side = "WATCH"

            gate_pass = side in ("BUY_YES", "BUY_NO")
            if gate_pass:
                reason_codes.append("GATE_PASS")

            # Sizing
            size_rec = 0.0
            if gate_pass:
                for conf_thresh, size in _SIZE_TIERS:
                    if confidence >= conf_thresh:
                        size_rec = size
                        break
                if size_rec == 0.0:
                    size_rec = 5.0

            # Execution hint
            if side == "BUY_YES":
                exec_hint = f"BUY YES @ {yes_ask:.2f}"
            elif side == "BUY_NO":
                exec_hint = f"BUY NO @ {no_ask:.2f}"
            else:
                exec_hint = "WATCH"

            size_cap = min(depth_for_side * 0.10, 25.0) if gate_pass else 0.0

            # Event context
            event_ticker = self._extract_event_ticker(market_id)
            event = None
            if market_family_name != "windowed":
                event = self.event_detector.get_active_event(speaker, event_ticker)

            event_context: dict = {}
            time_remaining: float | None = None
            if event:
                event_context = {
                    "event_id": event.event_id,
                    "speech_state": event.speech_state,
                    "event_type": event.event_type,
                }
                if event.speech_state == "live":
                    reason_codes.append("LIVE")
                elif event.speech_state == "scheduled":
                    reason_codes.append("PRE_EVENT")
                elif event.speech_state == "ended":
                    reason_codes.append("EVENT_ENDED")
            event_context["market_family"] = market_family_name
            event_context["series_ticker"] = series_ticker

            # If event ended and market pricing YES high, don't bet NO
            if (side == "BUY_NO"
                    and "EVENT_ENDED" in reason_codes
                    and yes_ask >= 0.70):
                reason_codes.append("ENDED_EVENT_BULLISH_BLOCK")
                side = "WATCH"

            # Phrase hit confirmation — near-certain YES
            if hit_today:
                reason_codes.append("PHRASE_HIT")
                p_model = 0.98
                ev_yes = round(p_model - yes_ask, 4)
                ev_no = round((1.0 - p_model) - no_ask, 4)
                if ev_yes > 0 and yes_ask < 0.95:
                    side = "BUY_YES"
                    reason_codes.append("CI_EXCLUDES_MARKET_YES")
                    gate_pass = True

            rationale = (
                f"side={side} p={p_model:.3f} ci=[{ci_low:.3f},{ci_high:.3f}] "
                f"reasons={','.join(reason_codes)}; "
                f"ev_yes={ev_yes:+.4f} ev_no={ev_no:+.4f}; "
                f"ask_yes={yes_ask:.2f} ask_no={no_ask:.2f}"
            )

            score_payload: dict = {
                "p_model": round(p_model, 4),
                "ci_low": round(ci_low, 4),
                "ci_high": round(ci_high, 4),
                "confidence": round(confidence, 3),
                "ev_yes": ev_yes,
                "ev_no": ev_no,
                "kelly_fraction": components.get("kelly_fraction"),
                **{k: v for k, v in components.items()
                   if k not in ("ev_yes", "ev_no", "kelly_fraction")},
            }

            payload = {
                "ts": now,
                "market_id": market_id,
                "subject": speaker,
                "phrase": phrase,
                "side": side,
                "p_literal": round(p_model, 4),
                "p_calibrated": round(p_model, 4),
                "scores": score_payload,
                "liquidity": {
                    "yes_ask": round(yes_ask, 4),
                    "no_ask": round(no_ask, 4),
                    "spread": round(yes_spread, 4),
                    "yes_spread": round(yes_spread, 4),
                    "depth_yes": depth_yes,
                    "depth_no": depth_no,
                    "spread_ok": yes_spread <= self.max_spread,
                    "depth_ok": depth_for_side >= self.min_depth,
                    "gate_pass": gate_pass,
                },
                "yes_ask": round(yes_ask, 4),
                "no_ask": round(no_ask, 4),
                "ev_yes": ev_yes,
                "ev_no": ev_no,
                "score_confidence": round(confidence, 3),
                "exec_price_hint": exec_hint,
                "size_rec": size_rec,
                "size_cap": size_cap,
                "spread_ok": yes_spread <= self.max_spread,
                "depth_ok": depth_for_side >= self.min_depth,
                "gate_pass": gate_pass,
                "reason_codes": reason_codes,
                "event": event_context,
                "time_remaining_sec": time_remaining,
                "rationale": rationale,
            }

            is_material = self._is_material_change(
                market_id, side, ev_yes, ev_no, now_dt
            )

            _evt_key = event_ticker or market_family_name or "unknown"
            _ev_chosen = (
                ev_yes if side == "BUY_YES"
                else ev_no if side == "BUY_NO"
                else 0.0
            )
            pending.append((_evt_key, _ev_chosen, payload, is_material))

        # Pass 2: event bet cap (portfolio correlation protection)
        _event_buy_counts: dict[str, int] = defaultdict(int)
        _event_risk_dollars: dict[str, float] = defaultdict(float)
        pending.sort(
            key=lambda x: (
                x[0],
                0 if x[2]["side"] in ("BUY_YES", "BUY_NO") else 1,
                -x[1],
            )
        )

        notified = 0
        for _evt_key, _ev_chosen, payload, is_material in pending:
            _side = payload["side"]
            if _side in ("BUY_YES", "BUY_NO"):
                _price = float(
                    payload["yes_ask"] if _side == "BUY_YES"
                    else payload["no_ask"]
                )
                _size = float(payload.get("size_rec", 5.0))
                _bet_risk = _size * _price
                _count_exceeded = _event_buy_counts[_evt_key] >= MAX_BUY_PER_EVENT
                _risk_exceeded = (
                    (_event_risk_dollars[_evt_key] + _bet_risk) > MAX_RISK_PER_EVENT
                )
                if _count_exceeded or _risk_exceeded:
                    payload["side"] = "WATCH"
                    payload["gate_pass"] = False
                    rc = payload.get("reason_codes", [])
                    rc.append("EVENT_BET_CAP" if _count_exceeded else "EVENT_RISK_CAP")
                    payload["reason_codes"] = rc
                    _side = "WATCH"
                    is_material = False
                else:
                    _event_buy_counts[_evt_key] += 1
                    _event_risk_dollars[_evt_key] += _bet_risk

            # Write to DB
            _mkt_id = payload["market_id"]
            _new_side = payload["side"]
            _should_write = is_material or (
                self._last_recorded_side.get(_mkt_id) != _new_side
            )
            if _should_write:
                self.conn.execute(
                    """INSERT INTO action_cards (
                        ts, market_id, phrase, side, p_literal,
                        yes_ask, no_ask, ev_yes, ev_no,
                        exec_price_hint, size_cap,
                        spread_ok, depth_ok, gate_pass, rationale, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        payload["ts"],
                        payload["market_id"],
                        payload["phrase"],
                        payload["side"],
                        payload["p_literal"],
                        payload["yes_ask"],
                        payload["no_ask"],
                        payload["ev_yes"],
                        payload["ev_no"],
                        payload["exec_price_hint"],
                        payload["size_cap"],
                        int(payload["spread_ok"]),
                        int(payload["depth_ok"]),
                        int(payload["gate_pass"]),
                        payload["rationale"],
                        json.dumps(payload, ensure_ascii=True),
                    ),
                )
                self._last_recorded_side[_mkt_id] = _new_side
            append_jsonl(self.action_cards_path, payload)

            if is_material:
                print(format_card_oneliner(payload))
                if self.notifier and _side in ("BUY_YES", "BUY_NO"):
                    self.notifier.notify(payload)
                notified += 1
            count += 1

        self.conn.commit()
        if count > 0:
            logger.debug(
                "BayesianScorer: scored %d markets, %d material", count, notified
            )
        return count

    # --- Helper methods ---

    def _latest_snapshots(self) -> list:
        return self.conn.execute(
            """SELECT s.*
               FROM market_snapshots s
               INNER JOIN (
                   SELECT market_id, MAX(id) AS max_id
                   FROM market_snapshots
                   GROUP BY market_id
               ) latest ON latest.max_id = s.id
               ORDER BY s.market_id"""
        ).fetchall()

    def _market_hit_today(
        self, market_id: str, today: str
    ) -> tuple[bool, str | None]:
        phrases = self.market_phrases.get(market_id, [])
        if not phrases:
            return False, None

        placeholders = ",".join("?" for _ in phrases)
        row = self.conn.execute(
            f"""SELECT phrase FROM phrase_hits
                WHERE hit_date = ?
                  AND phrase IN ({placeholders})
                ORDER BY id DESC LIMIT 1""",
            [today, *phrases],
        ).fetchone()
        if row:
            return True, str(row["phrase"])
        return False, phrases[0]

    @staticmethod
    def _extract_event_ticker(market_id: str) -> str | None:
        parts = market_id.split("-")
        if len(parts) >= 3:
            return "-".join(parts[:2])
        return None

    def _is_material_change(
        self,
        market_id: str,
        side: str,
        ev_yes: float,
        ev_no: float,
        now_dt: datetime,
    ) -> bool:
        prior = self._prior_cards.get(market_id)
        ev_chosen = ev_yes if side == "BUY_YES" else ev_no if side == "BUY_NO" else 0.0

        if prior is None:
            self._prior_cards[market_id] = {
                "side": side, "ev": ev_chosen, "ts": now_dt
            }
            return side in ("BUY_YES", "BUY_NO")

        if prior["side"] != side:
            self._prior_cards[market_id] = {
                "side": side, "ev": ev_chosen, "ts": now_dt
            }
            return side in ("BUY_YES", "BUY_NO")

        if abs(ev_chosen - prior["ev"]) >= self._material_ev_delta:
            self._prior_cards[market_id] = {
                "side": side, "ev": ev_chosen, "ts": now_dt
            }
            return True

        elapsed = (now_dt - prior["ts"]).total_seconds()
        if elapsed >= self._cooldown_sec:
            self._prior_cards[market_id] = {
                "side": side, "ev": ev_chosen, "ts": now_dt
            }
            return True

        return False
