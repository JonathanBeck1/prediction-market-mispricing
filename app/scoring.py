from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.base_rates import BaseRateLookup
from app.calibration import PlattCalibrator, get_calibrator, kl_divergence, kelly_fraction
from app.card_formatter import format_card_oneliner
from app.event_context import compute_topic_relevance, load_event_titles
from app.event_detector import EventDetector, EventInfo
from app.kalshi_api import LiveMarketCatalog
from app.market_family import (
    market_family,
    remaining_events_in_window,
    remaining_window_fraction,
    series_ticker_from_market_id,
)
from app.notifier import WhatsAppNotifier
from app.live_settlements import LiveSettlements
from app.polymarket import PolymarketPrices
from app.signals import SignalModifiers, SignalStore
from app.event_signals import EventSignalStore
from app.utils import append_jsonl, utc_now_iso
from app.wallet_flow import WalletFlowSignals
from app.window_state import WindowStateCache, P_WINDOW_SETTLED_YES, P_WINDOW_SETTLED_NO
from app.phrase_hazard import PhraseHazardCache
from app.rolling_rates import RollingRatesCache
from app.price_velocity import PriceVelocityCache
from app.wh_schedule import WHScheduleCache
from app.phrase_trends import PhraseTrendCache
from app.phrase_cooccurrence import CooccurrenceCache
from app.bias_map import BiasMapCache
from app.signal_learner import SignalWeightStore
from app.bayesian_rates import BayesianRateStore
from app.phrase_correlation import PhraseCorrelationStore

logger = logging.getLogger(__name__)

EV_MIN = 0.10  # Raised from 0.03, lowered from 0.15: sweet spot with CHEAP_NO + EXPENSIVE_NO gates
P_HIT = 0.98
P_ENDED = 0.02
P_UNKNOWN = 0.15
P_NO_EVENT_BASE = 0.10
DECAY_EXPONENT = 2
MIN_DECAY = 0.05
P_MIN = 0.02
P_MAX = 0.98

# ¼ Kelly fraction minimum — reject any bet whose Kelly fraction is below this.
# kelly_yes = ev_yes / (1 - yes_ask); kelly_no = ev_no / (1 - no_ask).
# Below 5% means the model's edge is too thin relative to bet-size risk.
KELLY_MIN_FRACTION = 0.05

# Pre-event vs live strategy differentiation.
# Pre-event bets use historical base rates only — no live signal available.
# Live bets have time-decay signal + phrase-hit confirmation.
# Evidence (374 outcomes): live BUY_NO → 57.2% WR; live BUY_YES → 37.8% WR.
# Separate EV thresholds ensure we only bet when conviction is warranted.
PRE_EVENT_EV_THRESHOLD  = 0.10   # Same bar as live YES — pre-event filter is already handled
                                  # by PRE_EVENT_NO_BLOCK for BUY_NO and YES_PRICE_FLOOR for YES
LIVE_BUY_YES_EV_MIN     = 0.10   # Standard bar for live YES bets
LIVE_BUY_YES_ASK_FLOOR  = 0.35   # Live BUY_YES floor — 35-45¢ range has 55.6% WR (data)

# Signal correlation dampening — prevents compounding of signals from same source
# (e.g. news + LLM + event_LLM all firing on the same headline).
SIGNAL_CORRELATION_THRESHOLD = 1.1   # Both must exceed this for dampening
SIGNAL_MAX_COMBINED_BOOST     = 2.0   # Cap for combined LLM × event_LLM effect
SIGNAL_TOTAL_BOOST_CAP        = 2.5   # Absolute cap on news × llm × event_llm

# BUY_NO requires 1.5x more EV than BUY_YES to trigger.  Live data (178 bets)
# shows BUY_YES edge is genuine (67% WR when model disagrees with market) while
# BUY_NO edge is much weaker (market right 73% when it's bullish vs our NO).
NO_EV_PREMIUM = 1.5

# Sports/broadcast speakers — their phrases are event-specific broadcast terms
# (arena names, game actions) that are NOT covered by the global political news
# signals.yaml.  Applying political LLM boosts to "grand slam" or "buzzer beater"
# adds noise, not signal.  Global signals are skipped for these speakers;
# per-event signals (from analyze_event.py) still apply.
_SPORTS_SPEAKERS: frozenset[str] = frozenset({
    "nba", "mlb", "ncaab", "mma", "nfl", "nhl", "earnings",
})


def _provenance_trim_story_id(sigs: SignalModifiers) -> str | None:
    """Eligible story id for SAME_STORY_PROVENANCE_TRIM (LLM tags, news RSS, or overlap)."""
    ns = set(sigs.news_story_hashes)
    ls = set(sigs.source_story_hashes)
    inter = ns & ls
    if inter:
        return sorted(inter)[0]
    if len(sigs.source_story_hashes) == 1:
        return sigs.source_story_hashes[0]
    if len(sigs.news_story_hashes) == 1:
        return sigs.news_story_hashes[0]
    return None


# Series that are structurally incompatible with phrase-mention scoring and
# should be skipped entirely.  Duration markets ask "will the briefing last
# X+ minutes?" — the threshold number is treated as a literal phrase by our
# model, producing nonsensical p_literal ≈ 2% for all thresholds.
_UNSCORABLE_SERIES_PREFIXES: frozenset[str] = frozenset({
    "KXLEAVITTMENTIONDURATION",
    "KXTRUMPMENTIONDURATION",
})


@dataclass(frozen=True)
class _PriorCard:
    side: str
    ev_yes: float
    ev_no: float
    speech_state: str | None
    ts: datetime


@dataclass
class ScoringEngine:
    conn: sqlite3.Connection
    action_cards_path: Path
    event_detector: EventDetector
    base_rates: BaseRateLookup
    signals: SignalStore
    max_spread: float = 0.15
    min_depth: float = 50.0
    ev_threshold: float = EV_MIN
    decay_exponent: float = DECAY_EXPONENT
    min_decay: float = MIN_DECAY
    cooldown_sec: float = 120.0
    material_ev_delta: float = 0.03
    notifier: WhatsAppNotifier | None = None
    market_phrases: dict[str, list[str]] = field(default_factory=dict, repr=False)
    focus_event_markets: bool = False
    pre_event_window_sec: float = 21600.0
    poly_prices: PolymarketPrices | None = None
    poly_blend_weight: float = 0.3
    poly_min_confidence: float = 0.35
    live_settlements: LiveSettlements | None = None
    window_state: WindowStateCache | None = None
    rolling_rates: RollingRatesCache | None = None
    price_velocity: PriceVelocityCache | None = None
    wh_schedule: WHScheduleCache | None = None
    phrase_trends: PhraseTrendCache | None = None
    cooccurrence: CooccurrenceCache | None = None
    bias_map: BiasMapCache | None = None
    wallet_signals: WalletFlowSignals | None = None
    event_signals: EventSignalStore | None = None
    phrase_hazard: PhraseHazardCache | None = None
    signal_weights: SignalWeightStore | None = None
    bayesian_rates: BayesianRateStore | None = None
    phrase_correlations: PhraseCorrelationStore | None = None
    wallet_flow_weight: float = 0.12
    wallet_min_confidence: float = 0.35
    adaptive_ev_scale: float = 0.05
    source_agreement_weight: float = 0.04
    market_veto_margin: float = 0.10  # legacy field; no longer read by scoring logic (replaced by specific gate constants)
    pre_event_yes_threshold: float = 0.08  # raised from EV_MIN (0.03): pre-event needs stronger YES signal
    block_off_topic_yes: bool = False
    penny_price_threshold: float = 0.0
    event_titles_refresh_sec: float = 300.0
    market_catalog: LiveMarketCatalog | None = None
    outcomes_cache_path: Path = Path("data/kalshi_outcomes.json")
    calibrator: PlattCalibrator | None = None
    series_prior_strength: float = 4.0
    market_catalog_refresh_sec: float = 300.0
    outcomes_refresh_sec: float = 900.0
    _event_titles: dict[str, str] = field(default_factory=dict, repr=False)
    _event_titles_last_load: datetime | None = field(default=None, repr=False)
    _market_catalog_last_load: datetime | None = field(default=None, repr=False)
    _outcomes_last_load: datetime | None = field(default=None, repr=False)
    _prior: dict[str, _PriorCard] = field(default_factory=dict, repr=False)
    _market_series: dict[str, str] = field(default_factory=dict, repr=False)
    _market_close_times: dict[str, str] = field(default_factory=dict, repr=False)
    _series_phrase_counts: dict[tuple[str, str, str], dict[str, int]] = field(default_factory=dict, repr=False)
    _series_totals: dict[tuple[str, str], dict[str, int]] = field(default_factory=dict, repr=False)
    # Dedup cache: last recorded side per market.  We only INSERT into action_cards
    # when the recommendation is actionable (BUY_YES/BUY_NO) OR when the side has
    # changed since the last write.  This prevents the table growing to millions of
    # WATCH rows per day and is the primary control on DB size.
    _last_recorded_side: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._prime_market_metadata()
        self._load_series_outcomes()
        if self._event_titles:
            self._event_titles_last_load = datetime.now(tz=timezone.utc)
        else:
            self._refresh_event_titles(force=True)
        if self.calibrator is None:
            object.__setattr__(self, "calibrator", get_calibrator())

    def _prime_market_metadata(self) -> None:
        if not self.market_catalog:
            return
        self._market_series = {}
        self._market_close_times = {}
        for market in self.market_catalog.markets:
            series = market.series_ticker or series_ticker_from_market_id(market.ticker)
            self._market_series[market.ticker] = series
            self._market_close_times[market.ticker] = market.close_time
            variants = [v.strip().lower() for v in market.phrase_variants if str(v).strip()]
            if variants:
                self.market_phrases[market.ticker] = variants
        self._market_catalog_last_load = datetime.now(tz=timezone.utc)

    def _load_series_outcomes(self) -> None:
        if not self.outcomes_cache_path.exists():
            return
        try:
            data = json.loads(self.outcomes_cache_path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Could not load outcomes cache from %s", self.outcomes_cache_path)
            return

        phrase_counts: dict[tuple[str, str, str], dict[str, int]] = defaultdict(
            lambda: {"yes": 0, "no": 0}
        )
        series_totals: dict[tuple[str, str], dict[str, int]] = defaultdict(
            lambda: {"yes": 0, "no": 0}
        )
        for market in data.get("markets", []):
            result = market.get("result")
            if result not in {"yes", "no"}:
                continue
            speaker = str(market.get("speaker", "")).strip()
            phrase = str(market.get("primary_phrase", "")).strip().lower()
            series = str(market.get("series_ticker", "")).strip().upper()
            if not speaker or not phrase or not series:
                continue
            phrase_counts[(speaker, series, phrase)][result] += 1
            series_totals[(speaker, series)][result] += 1
        self._series_phrase_counts = dict(phrase_counts)
        self._series_totals = dict(series_totals)
        self._outcomes_last_load = datetime.now(tz=timezone.utc)

    def _refresh_market_catalog(self) -> None:
        now_dt = datetime.now(tz=timezone.utc)
        if self._market_catalog_last_load is not None:
            elapsed = (now_dt - self._market_catalog_last_load).total_seconds()
            if elapsed < self.market_catalog_refresh_sec:
                return
        catalog = LiveMarketCatalog.from_cache()
        if not catalog.markets:
            self._market_catalog_last_load = now_dt
            return
        self.market_catalog = catalog
        self._prime_market_metadata()

    def _refresh_outcomes_cache(self) -> None:
        now_dt = datetime.now(tz=timezone.utc)
        if self._outcomes_last_load is not None:
            elapsed = (now_dt - self._outcomes_last_load).total_seconds()
            if elapsed < self.outcomes_refresh_sec:
                return
        self._load_series_outcomes()
        # Reload window state alongside outcomes so settled phrases stay current
        if self.window_state is not None:
            self.window_state = WindowStateCache.from_cache()
        # Reload price velocity — data/price_velocity.json is re-written every 5 min
        # by compute_price_velocity.py.  Reload here keeps signals fresh without
        # a dedicated reload timer (outcomes_refresh_sec already limits frequency).
        if self.price_velocity is not None:
            self.price_velocity = PriceVelocityCache.from_cache()
        # Reload WH schedule if stale (written every 30 min by fetch_wh_schedule.py)
        if self.wh_schedule is not None and self.wh_schedule.is_stale():
            self.wh_schedule = WHScheduleCache.load()

    def _refresh_event_titles(self, *, force: bool = False) -> None:
        now_dt = datetime.now(tz=timezone.utc)
        if not force and self._event_titles_last_load is not None:
            elapsed = (now_dt - self._event_titles_last_load).total_seconds()
            if elapsed < self.event_titles_refresh_sec:
                return
        self._event_titles = load_event_titles()
        self._event_titles_last_load = now_dt

    @staticmethod
    def _extract_event_ticker(market_id: str) -> str | None:
        """Derive the event ticker from a market_id like KXTRUMPMENTION-26MAR07-MOG."""
        parts = market_id.split("-")
        if len(parts) >= 3:
            return "-".join(parts[:2])
        return None

    def _market_hit_today(self, market_id: str, today: str) -> tuple[bool, str | None]:
        phrases = self.market_phrases.get(market_id, [])
        if not phrases:
            return False, None

        placeholders = ",".join("?" for _ in phrases)
        row = self.conn.execute(
            f"""
            SELECT phrase
            FROM phrase_hits
            WHERE hit_date = ?
              AND phrase IN ({placeholders})
            ORDER BY id DESC
            LIMIT 1
            """,
            [today, *phrases],
        ).fetchone()
        if row:
            return True, str(row["phrase"])
        return False, phrases[0]

    def _latest_snapshots(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT s.*
            FROM market_snapshots s
            INNER JOIN (
                SELECT market_id, MAX(id) AS max_id
                FROM market_snapshots
                GROUP BY market_id
            ) latest ON latest.max_id = s.id
            ORDER BY s.market_id
            """
        ).fetchall()

    def _series_ticker(self, market_id: str) -> str:
        return self._market_series.get(market_id) or series_ticker_from_market_id(market_id)

    def _market_family(self, market_id: str) -> str:
        return market_family(self._series_ticker(market_id))

    def _historical_series_base_rate(self, speaker: str, series_ticker: str, phrase: str) -> float | None:
        key = (speaker, series_ticker, phrase.lower())
        counts = self._series_phrase_counts.get(key)
        totals = self._series_totals.get((speaker, series_ticker))
        series_prior: float | None = None
        if totals:
            total_n = totals["yes"] + totals["no"]
            if total_n > 0:
                series_prior = (totals["yes"] + 1.0) / (total_n + 2.0)
        if counts:
            total_n = counts["yes"] + counts["no"]
            if total_n > 0:
                prior = series_prior if series_prior is not None else self.base_rates.get(speaker, "general", phrase)
                return (counts["yes"] + self.series_prior_strength * prior) / (
                    total_n + self.series_prior_strength
                )
        return series_prior

    def _remaining_window_fraction(self, market_id: str, now_dt: datetime) -> float | None:
        """Legacy time-fraction; prefer _remaining_events_n()."""
        close_time = self._market_close_times.get(market_id)
        return remaining_window_fraction(
            close_time,
            series_ticker=self._series_ticker(market_id),
            now_dt=now_dt,
        )

    def _remaining_events_n(self, market_id: str, now_dt: datetime) -> float | None:
        """Return estimated remaining event count N for window markets.

        Uses series-calibrated events-per-day rates so that the compound formula
        p = 1 - (1-p_per_event)^N reflects real speech/game cadence rather than
        raw calendar time.  Returns None for single-event markets.
        """
        close_time = self._market_close_times.get(market_id)
        return remaining_events_in_window(
            close_time,
            series_ticker=self._series_ticker(market_id),
            now_dt=now_dt,
        )

    def _compute_window_p_literal(
        self,
        hit_today: bool,
        speaker: str,
        phrase: str,
        now_dt: datetime,
        market_id: str,
        event_ticker: str | None = None,
    ) -> tuple[float, list[str], dict]:
        components: dict = {"base_rate": None, "time_decay": None, "news_pressure": 1.0, "x_buzz": 1.0}
        if hit_today:
            return P_HIT, ["PHRASE_HIT"], components

        # ── Window already-resolved check ─────────────────────────────────────
        # If this exact phrase already settled YES or NO within the same window
        # (from outcomes history or live settlements), clamp p_literal and skip
        # the full scoring path — the market should already be closed but may
        # appear in stale cache or scoring queue.
        et = event_ticker or ""
        if not et:
            # Derive event_ticker from market_id (strip last segment)
            parts = market_id.rsplit("-", 1)
            et = parts[0] if len(parts) == 2 else market_id

        if self.window_state and et:
            settled_yes, settled_no = self.window_state.check_phrase(et, phrase)
            if settled_yes:
                components["window_settled"] = "yes"
                return P_WINDOW_SETTLED_YES, ["WINDOW_SETTLED_YES"], components
            if settled_no:
                components["window_settled"] = "no"
                return P_WINDOW_SETTLED_NO, ["WINDOW_SETTLED_NO"], components

        sigs = self.signals.get(phrase)
        news = sigs.news_pressure
        buzz = sigs.x_buzz
        # Skip global political LLM signals for sports/broadcast speakers —
        # their phrases are broadcast-specific and unrelated to news signals.
        _is_sports = (speaker or "").lower().strip() in _SPORTS_SPEAKERS
        llm  = 1.0 if _is_sports else sigs.llm_boost
        components["news_pressure"] = news
        components["x_buzz"] = buzz
        if llm != 1.0:
            components["llm_boost"] = llm
            if sigs.llm_reasoning:
                components["llm_reasoning"] = sigs.llm_reasoning
            if sigs.llm_evidence:
                components["llm_evidence"] = sigs.llm_evidence
            if sigs.llm_topic:
                components["llm_topic"] = sigs.llm_topic
        if sigs.source_story_hashes:
            components["source_story_hashes"] = list(sigs.source_story_hashes)
        if sigs.news_story_hashes:
            components["news_story_hashes"] = list(sigs.news_story_hashes)

        # ── Adaptive signal weighting ─────────────────────────────────────────
        # Apply learned weights from outcome data: down-weight signals that have
        # been losing money for this speaker, up-weight signals that are profitable.
        _spk_key = (speaker or "").lower().strip()
        if self.signal_weights is not None:
            _sw_news = self.signal_weights.get_weight(_spk_key, "news_pressure_buy_yes")
            _sw_llm = self.signal_weights.get_weight(_spk_key, "llm_boost" if llm > 1.0 else "llm_suppress")
            if news != 1.0:
                news = 1.0 + (news - 1.0) * _sw_news
            if llm != 1.0:
                llm = 1.0 + (llm - 1.0) * _sw_llm
            if _sw_news != 1.0 or _sw_llm != 1.0:
                components["adaptive_signal_weights"] = {
                    "news": round(_sw_news, 3), "llm": round(_sw_llm, 3),
                }

        # Window markets: apply per-event LLM if available
        # Skip event_llm for sports — same rationale as global LLM bypass:
        # sports broadcast phrases are unrelated to political news analysis.
        event_llm = 1.0
        if not _is_sports and self.event_signals and et:
            window_event = self.event_detector.get_active_event(speaker, et)
            if window_event is not None:
                event_llm, _ = self.event_signals.get(window_event.event_id, phrase)
        if event_llm != 1.0:
            # Apply adaptive weight to event_llm
            if self.signal_weights is not None:
                _sw_evt = self.signal_weights.get_weight(
                    _spk_key,
                    "event_llm_boost" if event_llm > 1.0 else "event_llm_suppress",
                )
                event_llm = 1.0 + (event_llm - 1.0) * _sw_evt
            components["event_llm"] = round(event_llm, 3)

        series = self._series_ticker(market_id)
        base = self._historical_series_base_rate(speaker, series, phrase)
        if base is None:
            base = self.base_rates.get(speaker, "general", phrase)

        # Initialize reasons BEFORE any appends below to avoid NameError
        reasons = ["WINDOW_MARKET"]
        if (speaker, series, phrase.lower()) in self._series_phrase_counts:
            reasons.append("SERIES_HISTORY")

        # ── Bias map override (series-specific empirical rate) ────────────────
        # Most precise base rate source: series+phrase empirical from 9k+ resolutions.
        # Overrides the speaker+context bucket rate when available (N >= 10).
        if self.bias_map is not None:
            _bm_rate = self.bias_map.get_empirical_rate(series, phrase)
            if _bm_rate is not None:
                components["base_rate_bias_map"] = round(_bm_rate, 4)
                base = _bm_rate
                reasons.append("BIAS_MAP_RATE")

        # ── Rolling N-speech blend (window markets) ───────────────────────────
        if self.rolling_rates:
            roll_result = self.rolling_rates.blend(speaker, phrase, base)
            if roll_result is not None:
                blended_base, roll_tag = roll_result
                components["base_rate_historical"] = round(base, 4)
                base = blended_base

        # ── Enhanced double-counting dampening (window markets) ─────────────────
        _window_correlation_detected = False

        _wnews_pre = news
        _wllm_pre = llm

        # Apply same correlation detection as single-event markets
        if news > SIGNAL_CORRELATION_THRESHOLD and llm > SIGNAL_CORRELATION_THRESHOLD:
            if llm > news:
                news = 1.0 + (news - 1.0) * 0.5
            else:
                llm = 1.0 + (llm - 1.0) * 0.5
            _window_correlation_detected = True

        _prov_id_w = _provenance_trim_story_id(sigs)
        if (
            _wnews_pre > SIGNAL_CORRELATION_THRESHOLD
            and _wllm_pre > SIGNAL_CORRELATION_THRESHOLD
            and _prov_id_w is not None
        ):
            if llm > news:
                news = 1.0 + (news - 1.0) * 0.85
            else:
                llm = 1.0 + (llm - 1.0) * 0.85
            components["same_story_provenance_trim"] = _prov_id_w
            _window_correlation_detected = True
            reasons.append("SAME_STORY_PROVENANCE_TRIM")
            
        if (llm > SIGNAL_CORRELATION_THRESHOLD and event_llm > SIGNAL_CORRELATION_THRESHOLD 
                and (llm > 1.0) == (event_llm > 1.0)):
            combined_llm = llm * event_llm
            if combined_llm > SIGNAL_MAX_COMBINED_BOOST:
                scale_factor = SIGNAL_MAX_COMBINED_BOOST / combined_llm
                llm *= scale_factor
                event_llm *= scale_factor
                _window_correlation_detected = True
                
        if _window_correlation_detected:
            components["signal_correlation_capped"] = True
            reasons.append("SIGNAL_CORRELATION_CAPPED")

        p_full = _clamp_p(base * news * buzz * llm * event_llm)
        # Use event-count N (speeches/games remaining) not raw time-fraction.
        # p_per_event = probability the phrase is said in a single speech/game.
        # Compound: p_window = 1 - (1 - p_per_event)^N
        n_events = self._remaining_events_n(market_id, now_dt)
        if n_events is not None:
            components["events_remaining"] = round(n_events, 2)
            p = 1.0 - (1.0 - p_full) ** max(0.0, n_events)
            if n_events <= 1.0:
                reasons.append("WINDOW_ENDING_SOON")
        else:
            # Fallback to legacy time-fraction for series without rate data
            frac_remaining = self._remaining_window_fraction(market_id, now_dt)
            if frac_remaining is not None:
                components["window_fraction_remaining"] = round(frac_remaining, 4)
                p = 1.0 - (1.0 - p_full) ** frac_remaining
                if frac_remaining <= 0.10:
                    reasons.append("WINDOW_ENDING_SOON")
            else:
                p = p_full

        # ── Window pace signal ────────────────────────────────────────────────
        # If phrases are resolving faster than historical average this window,
        # apply a small boost to remaining open phrases.
        if self.window_state and et:
            pace_adj = self.window_state.pace_signal(et)
            if abs(pace_adj) >= 0.005:
                p = _clamp_p(p + pace_adj)
                components["window_pace"] = round(pace_adj, 4)
                if pace_adj > 0:
                    reasons.append("WINDOW_ACTIVE_PACE")

        # ── Co-occurrence lift (window markets) ──────────────────────────────
        # If sibling phrases already settled YES this window, boost/suppress
        # the target phrase using the empirical co-occurrence lift matrix.
        if self.cooccurrence and self.window_state and et:
            ws = self.window_state.get_state(et)
            if ws is not None and ws.yes_phrases:
                confirmed = list(ws.yes_phrases)
                lift, triggers = self.cooccurrence.get_confirmed_phrases_for_event(
                    speaker, confirmed, phrase
                )
                if lift != 1.0:
                    p = _clamp_p(p * lift)
                    components["cooccurrence_lift"]     = round(lift, 3)
                    components["cooccurrence_triggers"] = triggers[:3]
                    reasons.append("COOCCUR_BOOST" if lift > 1 else "COOCCUR_SUPPRESS")

        components["base_rate"] = round(base, 4)
        self._add_signal_reasons(reasons, news, buzz, llm, event_llm)
        return _clamp_p(p), reasons, components

    def _compute_p_literal(
        self,
        hit_today: bool,
        speaker: str,
        event: EventInfo | None,
        phrase: str,
        now_dt: datetime,
        event_ticker: str | None = None,
    ) -> tuple[float, list[str], dict]:
        """Returns (p_literal, reason_codes, score_components)."""
        components: dict = {"base_rate": None, "time_decay": None, "news_pressure": 1.0, "x_buzz": 1.0}

        if hit_today:
            return P_HIT, ["PHRASE_HIT"], components

        sigs = self.signals.get(phrase)
        news = sigs.news_pressure
        buzz = sigs.x_buzz
        # Skip global political LLM signals for sports/broadcast speakers.
        _is_sports = (speaker or "").lower().strip() in _SPORTS_SPEAKERS
        llm  = 1.0 if _is_sports else sigs.llm_boost
        components["news_pressure"] = news
        components["x_buzz"] = buzz
        if llm != 1.0:
            components["llm_boost"] = llm
            if sigs.llm_reasoning:
                components["llm_reasoning"] = sigs.llm_reasoning
            if sigs.llm_evidence:
                components["llm_evidence"] = sigs.llm_evidence
            if sigs.llm_topic:
                components["llm_topic"] = sigs.llm_topic
        if sigs.source_story_hashes:
            components["source_story_hashes"] = list(sigs.source_story_hashes)
        if sigs.news_story_hashes:
            components["news_story_hashes"] = list(sigs.news_story_hashes)

        # ── Adaptive signal weighting (single-event) ──────────────────────────
        _spk_key = (speaker or "").lower().strip()
        if self.signal_weights is not None:
            _sw_news = self.signal_weights.get_weight(_spk_key, "news_pressure_buy_yes")
            _sw_llm = self.signal_weights.get_weight(_spk_key, "llm_boost" if llm > 1.0 else "llm_suppress")
            if news != 1.0:
                news = 1.0 + (news - 1.0) * _sw_news
            if llm != 1.0:
                llm = 1.0 + (llm - 1.0) * _sw_llm
            if _sw_news != 1.0 or _sw_llm != 1.0:
                components["adaptive_signal_weights"] = {
                    "news": round(_sw_news, 3), "llm": round(_sw_llm, 3),
                }

        # ── Per-event LLM multiplier ──────────────────────────────────────────
        # Skip event_llm for sports — same rationale as global LLM bypass.
        event_llm = 1.0
        event_llm_reason = ""
        if not _is_sports and self.event_signals and event is not None:
            event_llm, event_llm_reason = self.event_signals.get(event.event_id, phrase)
            # Apply adaptive weight to event_llm
            if event_llm != 1.0 and self.signal_weights is not None:
                _sw_evt = self.signal_weights.get_weight(
                    _spk_key,
                    "event_llm_boost" if event_llm > 1.0 else "event_llm_suppress",
                )
                event_llm = 1.0 + (event_llm - 1.0) * _sw_evt
        if event_llm != 1.0:
            components["event_llm"] = round(event_llm, 3)
            if event_llm_reason:
                components["event_llm_reason"] = event_llm_reason

        # ── Hard p_override: bypass frequency formula for causally certain phrases ──
        # Used when the event's purpose structurally guarantees a phrase will appear
        # (e.g. the name of the person being sworn in at a swearing-in ceremony, or
        # the title of a bill at a signing ceremony). Set in the event signal JSON
        # under "p_overrides": {"phrase": 0.95} or via events.yaml p_overrides.
        if self.event_signals and event is not None:
            p_ov = self.event_signals.get_override(event.event_id, phrase)
            if p_ov is not None:
                p_ov = _clamp_p(p_ov)
                components["p_override"] = round(p_ov, 4)
                return p_ov, ["CAUSAL_OVERRIDE"], components

        reasons: list[str] = []

        topic_rel = 1.0
        if event_ticker and phrase:
            event_title = self._event_titles.get(event_ticker, "")
            if event_title:
                topic_rel = compute_topic_relevance(event_title, phrase)
                if topic_rel != 1.0:
                    components["topic_relevance"] = round(topic_rel, 2)
                    components["event_title"] = event_title
                    if topic_rel >= 1.1:
                        reasons.append("ON_TOPIC")
                    elif topic_rel <= 0.30:
                        reasons.append("OFF_TOPIC")

        if event is None:
            p = P_NO_EVENT_BASE * topic_rel * news * buzz * llm * event_llm
            components["base_rate"] = P_NO_EVENT_BASE
            reasons.append("NO_EVENT")
            self._add_signal_reasons(reasons, news, buzz, llm, event_llm)
            return _clamp_p(p), reasons, components  # no floors without event context

        event_type = event.event_type

        # ── WH Schedule: event_type override ─────────────────────────────────
        # If the WH schedule confirms a signing / remarks / briefing within the
        # last 24 h, override "general" with the confirmed type so the correct
        # context-specific base rates are used.
        wh_keyword_boost = 0.0
        if self.wh_schedule is not None:
            refined_type = self.wh_schedule.resolve_event_type(
                event_type, lookahead_hours=24.0, now=now_dt
            )
            if refined_type != event_type:
                components["wh_event_type_override"] = refined_type
                components["wh_event_type_was"] = event_type
                reasons.append("WH_CONTEXT")
                event_type = refined_type
            # Keyword boost: if this phrase appears in a recent WH event title
            wh_keyword_boost = self.wh_schedule.keyword_boost(phrase, hours=6.0, now=now_dt)
            if wh_keyword_boost:
                components["wh_keyword_boost"] = round(wh_keyword_boost, 3)

        # Snapshot before double-count / correlation dampening — used for
        # SAME_STORY_PROVENANCE_TRIM gate (arms can fall below 1.1 after passes).
        _news_prov_gate = news
        _llm_prov_gate = llm

        # ── Enhanced double-counting dampening ────────────────────────────────
        # news_pressure, llm_boost (global), event_llm, and wh_keyword_boost can
        # all react to the same underlying headline within the same news cycle.
        # Enhanced (Session 65): more aggressive correlation detection and capping.
        # When ≥2 boost signals are simultaneously elevated, the weaker ones are
        # dampened to 35% of their marginal effect.
        # Suppression signals (< 1.0) are intentionally left undampened.
        _DOUBLE_COUNT_THRESHOLD = 1.05
        _DOUBLE_COUNT_WEIGHT    = 0.35

        _boost_signals: list[str] = []
        if news > _DOUBLE_COUNT_THRESHOLD:
            _boost_signals.append("news")
        if llm > _DOUBLE_COUNT_THRESHOLD:
            _boost_signals.append("llm")
        if event_llm > _DOUBLE_COUNT_THRESHOLD:
            _boost_signals.append("event_llm")

        if len(_boost_signals) >= 2:
            _all = {"news": news, "llm": llm, "event_llm": event_llm}
            _ranked = sorted(_boost_signals, key=lambda s: _all[s], reverse=True)
            for _s in _ranked[1:]:
                _orig = _all[_s]
                _dampened = 1.0 + (_orig - 1.0) * _DOUBLE_COUNT_WEIGHT
                if _s == "news":
                    news = _dampened
                elif _s == "llm":
                    llm = _dampened
                elif _s == "event_llm":
                    event_llm = _dampened
            components["double_count_dampened"] = _ranked[1:]
            if wh_keyword_boost > 0 and news > _DOUBLE_COUNT_THRESHOLD:
                _orig_wh = wh_keyword_boost
                wh_keyword_boost *= _DOUBLE_COUNT_WEIGHT
                components["wh_keyword_dampened"] = round(_orig_wh - wh_keyword_boost, 4)

        # ── Enhanced correlation detection (Session 65) ──────────────────────
        # Additional dampening for specific high-correlation combinations:
        # 1. News + LLM correlation (same headline triggers both)
        # 2. LLM + event_LLM agreement (redundant context analysis) 
        # 3. Total signal capping when too many signals boost simultaneously
        _correlation_detected = False

        # Correlation 1: News + LLM when both strongly elevated (same news story)
        if news > SIGNAL_CORRELATION_THRESHOLD and llm > SIGNAL_CORRELATION_THRESHOLD:
            # Dampen the weaker signal by 50%
            if llm > news:
                news = 1.0 + (news - 1.0) * 0.5
            else:
                llm = 1.0 + (llm - 1.0) * 0.5
            _correlation_detected = True

        # Correlation 1b: provenance (LLM [story:…], news RSS hash, or overlap) — extra trim
        _prov_id = _provenance_trim_story_id(sigs)
        if (
            _news_prov_gate > SIGNAL_CORRELATION_THRESHOLD
            and _llm_prov_gate > SIGNAL_CORRELATION_THRESHOLD
            and _prov_id is not None
        ):
            if llm > news:
                news = 1.0 + (news - 1.0) * 0.85
            else:
                llm = 1.0 + (llm - 1.0) * 0.85
            components["same_story_provenance_trim"] = _prov_id
            _correlation_detected = True
            reasons.append("SAME_STORY_PROVENANCE_TRIM")
            
        # Correlation 2: LLM + event_LLM agreement (redundant analysis)  
        if (llm > SIGNAL_CORRELATION_THRESHOLD and event_llm > SIGNAL_CORRELATION_THRESHOLD 
                and (llm > 1.0) == (event_llm > 1.0)):  # Same direction
            # Cap combined LLM effect at 2.0x
            combined_llm = llm * event_llm
            if combined_llm > SIGNAL_MAX_COMBINED_BOOST:
                scale_factor = SIGNAL_MAX_COMBINED_BOOST / combined_llm
                llm *= scale_factor
                event_llm *= scale_factor
                _correlation_detected = True
                
        # Total signal capping: prevent extreme over-boosting
        total_boost = news * llm * event_llm
        if total_boost > 2.5:  # Cap at 2.5x total boost
            scale_factor = 2.5 / total_boost
            news *= scale_factor
            llm *= scale_factor  
            event_llm *= scale_factor
            _correlation_detected = True
            
        if _correlation_detected:
            components["signal_correlation_capped"] = True
            reasons.append("SIGNAL_CORRELATION_CAPPED")

        base = self.base_rates.get(speaker, event_type, phrase)

        # ── Bias map override (series-specific empirical rate) ────────────────
        # For single-event markets, the series-specific empirical rate is
        # usually more accurate than the speaker+context bucket rate.
        if self.bias_map is not None and event_ticker:
            _series_for_bias = self._series_ticker(event_ticker) if event_ticker else ""
            if _series_for_bias:
                _bm_rate = self.bias_map.get_empirical_rate(_series_for_bias, phrase)
                if _bm_rate is not None:
                    components["base_rate_bias_map"] = round(_bm_rate, 4)
                    base = _bm_rate
                    reasons.append("BIAS_MAP_RATE")

        # ── Rolling N-speech blend ────────────────────────────────────────────
        # If the last 3-10 speeches show a meaningfully different rate than the
        # historical base, blend toward the recent trend.  This catches regime
        # changes (e.g. phrase fading out, or a new talking point emerging) that
        # the 90-day decay-weighted calibration reacts to slowly.
        #
        # SKIP for pre-event (scheduled) state: rolling rates are derived from
        # recent market resolution prices, which already reflect crowd consensus.
        # Applying them pre-event means chasing prices, not adding new information.
        # Live data: ROLLING_N5 had 15% WR and -$8.65 P&L on 87 pre-event bets —
        # the single worst pre-event signal.  Rolling is only useful during a live
        # speech where same-event pacing carries genuine causal signal.
        if self.rolling_rates and event.speech_state != "scheduled":
            roll_result = self.rolling_rates.blend(speaker, phrase, base)
            if roll_result is not None:
                blended_base, roll_tag = roll_result
                components["base_rate_historical"] = round(base, 4)
                components["base_rate_rolling"]    = round(blended_base, 4)
                base = blended_base
                reasons.append(roll_tag)

        # ── Bayesian rate uncertainty ─────────────────────────────────────────
        # The Bayesian posterior gives us both the rate AND our confidence in it.
        # High-confidence rates (many observations) get full weight.
        # Low-confidence rates (few observations) stay closer to the frequentist base.
        if self.bayesian_rates is not None:
            _bayes = self.bayesian_rates.get(speaker, phrase)
            if _bayes is not None:
                components["bayesian_mean"] = round(_bayes.mean, 4)
                components["bayesian_ci"] = [round(_bayes.ci_low, 4), round(_bayes.ci_high, 4)]
                components["bayesian_confidence"] = _bayes.confidence
                # Blend Bayesian rate toward base when confidence is high
                if _bayes.confidence >= 0.5 and _bayes.n_obs >= 10:
                    _blend_w = min(0.4, _bayes.confidence * 0.5)
                    base = (1 - _blend_w) * base + _blend_w * _bayes.mean
                    reasons.append("BAYESIAN_RATE")

        components["base_rate"] = round(base, 4)

        # ── Vocabulary trend multiplier ───────────────────────────────────────
        # Adjusts base rate for phrases whose 30d YES rate diverges from 90d by >15pp.
        # Applied as a small multiplier on top of base (before other signals).
        if self.phrase_trends is not None:
            _trend = self.phrase_trends.get_signal(speaker, phrase)
            if _trend.flag:
                _orig_base = base
                base = _clamp_p(base * _trend.multiplier)
                components["vocab_trend_flag"]  = _trend.flag
                components["vocab_trend_delta"] = round(_trend.delta, 4)
                components["vocab_trend_mult"]  = round(_trend.multiplier, 3)
                reasons.append(_trend.flag)

        if event.speech_state == "scheduled":
            p = _clamp_p(base * topic_rel * news * buzz * llm * event_llm + wh_keyword_boost)
            components["time_decay"] = 1.0
            reasons.append("PRE_EVENT")
            if wh_keyword_boost > 0:
                reasons.append("WH_KEYWORD")
            if components.get("double_count_dampened"):
                reasons.append("DOUBLE_COUNT_DAMPENED")
            if base * topic_rel >= 0.70:
                reasons.append("HIGH_BASE_RATE")
            elif base * topic_rel <= 0.20:
                reasons.append("LOW_BASE_RATE")
            self._add_signal_reasons(reasons, news, buzz, llm, event_llm)
            p = self._apply_p_floor(p, event, phrase, reasons, components)
            return p, reasons, components

        if event.speech_state == "live":
            elapsed = self._elapsed_sec(event, now_dt)
            duration = event.expected_duration_sec or 5400
            frac = min(elapsed / duration, 1.0) if duration > 0 else 0.0
            
            # Use empirical hazard rates if available, fallback to static decay
            if self.phrase_hazard is not None:
                decay = self.phrase_hazard.compute_survival_probability(
                    speaker, event.event_type, phrase, frac
                )
                if decay != max(self.min_decay, 1.0 - frac ** self.decay_exponent):
                    reasons.append("HAZARD_MODEL")
            else:
                decay = max(self.min_decay, 1.0 - frac ** self.decay_exponent)
            p = _clamp_p(base * topic_rel * decay * news * buzz * llm * event_llm + wh_keyword_boost)
            components["time_decay"] = round(decay, 4)
            reasons.append("LIVE")
            if wh_keyword_boost > 0:
                reasons.append("WH_KEYWORD")
            if components.get("double_count_dampened"):
                reasons.append("DOUBLE_COUNT_DAMPENED")
            if frac > 0.75:
                reasons.append("EVENT_ENDING_SOON")
            self._add_signal_reasons(reasons, news, buzz, llm, event_llm)

            # ── Co-occurrence lift (live events) ─────────────────────────────
            # Phrases confirmed YES earlier in this live event boost/suppress
            # the current phrase's probability via the empirical lift matrix.
            if self.cooccurrence and event is not None:
                confirmed_yes = list(getattr(event, "confirmed_yes_phrases", None) or [])
                if not confirmed_yes and self.window_state:
                    et_key = event_ticker or ""
                    if et_key:
                        ws = self.window_state.get_state(et_key)
                        if ws is not None:
                            confirmed_yes = list(ws.yes_phrases)
                if confirmed_yes:
                    lift, triggers = self.cooccurrence.get_confirmed_phrases_for_event(
                        speaker, confirmed_yes, phrase
                    )
                    if lift != 1.0:
                        p = _clamp_p(p * lift)
                        components["cooccurrence_lift"]     = round(lift, 3)
                        components["cooccurrence_triggers"] = triggers[:3]
                        reasons.append("COOCCUR_BOOST" if lift > 1 else "COOCCUR_SUPPRESS")

            p = self._apply_p_floor(p, event, phrase, reasons, components)
            return p, reasons, components

        if event.speech_state == "ended":
            reasons.append("EVENT_ENDED")
            return P_ENDED, reasons, components

        # unknown
        reasons.append("SIGNAL_UNKNOWN")
        return P_UNKNOWN, reasons, components

    def _apply_p_floor(
        self,
        p: float,
        event: "EventInfo",
        phrase: str,
        reasons: list[str],
        components: dict,
    ) -> float:
        """Raise p to event-specific p_floor if the floor exceeds the computed value.

        A p_floor is a domain-based minimum probability: the frequency formula
        may compute a very low probability for a phrase that is structurally
        elevated by the event context (e.g. "deport" has a 9% base rate but
        should be at least 60% at a DHS swearing-in ceremony).

        The floor is set in the event signal JSON under "p_floors": {"phrase": 0.60}
        and is generated by the LLM in analyze_event.py or authored manually.
        """
        if not (self.event_signals and event is not None):
            return p
        p_fl = self.event_signals.get_floor(event.event_id, phrase)
        if p_fl is not None and p < p_fl:
            p_fl_clamped = _clamp_p(p_fl)
            components["p_floor"] = round(p_fl_clamped, 4)
            components["p_before_floor"] = round(p, 4)
            reasons.append("P_FLOOR_APPLIED")
            return p_fl_clamped
        return p

    @staticmethod
    def _add_signal_reasons(
        reasons: list[str],
        news: float,
        buzz: float,
        llm: float = 1.0,
        event_llm: float = 1.0,
    ) -> None:
        if news > 1.2:
            reasons.append("NEWS_PRESSURE_HIGH")
        elif news < 0.8:
            reasons.append("NEWS_PRESSURE_LOW")
        if buzz > 1.2:
            reasons.append("X_BUZZ_HIGH")
        elif buzz < 0.8:
            reasons.append("X_BUZZ_LOW")
        if event_llm <= 0.6:
            reasons.append("EVENT_LLM_SUPPRESS")
        elif event_llm >= 1.3:
            reasons.append("EVENT_LLM_BOOST")
        if llm >= 1.3:
            reasons.append("LLM_BOOST_HIGH")
        elif llm >= 1.1:
            reasons.append("LLM_BOOST")
        elif llm <= 0.7:
            reasons.append("LLM_SUPPRESS_HIGH")
        elif llm <= 0.9:
            reasons.append("LLM_SUPPRESS")

    @staticmethod
    def _elapsed_sec(event: EventInfo, now_dt: datetime) -> float:
        start_ts = event.event_start_ts or event.scheduled_start_ts
        if not start_ts:
            return 0.0
        start = datetime.fromisoformat(start_ts)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return max(0.0, (now_dt - start).total_seconds())

    def _is_market_relevant(self, event: EventInfo | None, now_dt: datetime, market_id: str) -> bool:
        """When enabled, score only markets tied to active pre/live event windows."""
        if not self.focus_event_markets:
            return True
        if self._market_family(market_id) == "windowed":
            # Long-window contracts should remain visible even without a single active event.
            return True
        if event is None:
            return False

        state = event.speech_state
        if state in ("live", "unknown"):
            return True
        if state == "ended":
            return False
        if state != "scheduled":
            return False

        if not event.scheduled_start_ts:
            # If schedule is unknown, keep it visible rather than hiding silently.
            return True

        start = datetime.fromisoformat(event.scheduled_start_ts)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        starts_in = (start - now_dt).total_seconds()
        return 0 <= starts_in <= self.pre_event_window_sec

    def _compute_score_confidence(
        self,
        *,
        spread: float,
        depth_yes: float,
        depth_no: float,
        poly_confidence: float | None,
        wallet_confidence: float | None,
        reason_codes: list[str],
        p_literal: float = 0.5,
        yes_ask: float = 0.5,
    ) -> float:
        spread_cap = max(self.max_spread, 0.01)
        spread_quality = max(0.0, 1.0 - min(1.0, spread / spread_cap))

        depth_target = max(self.min_depth, 100.0)
        depth_quality = min(1.0, max(depth_yes, depth_no) / max(depth_target, 1.0))

        # Model-market divergence: when the model disagrees with the market,
        # that divergence IS the edge.  Live data (178 bets) shows BUY_YES
        # wins 67% when model says YES and market says NO — genuine alpha.
        # SCORE_CONF_HIGH previously used poly/wallet agreement (double-counting
        # signals already baked into p_literal) and had 17% WR — inversely
        # predictive.  Divergence replaces it.
        divergence = abs(p_literal - yes_ask)
        divergence_quality = min(1.0, divergence / 0.30)

        topic_quality = 0.75
        if "OFF_TOPIC" in reason_codes:
            topic_quality = 0.45
        elif "ON_TOPIC" in reason_codes:
            topic_quality = 1.0

        conf = (
            0.30 * spread_quality
            + 0.25 * depth_quality
            + 0.25 * divergence_quality
            + 0.20 * topic_quality
        )
        return round(max(0.20, min(1.0, conf)), 4)

    def _adaptive_ev_threshold(self, score_confidence: float) -> float:
        bump = (1.0 - score_confidence) * max(0.0, self.adaptive_ev_scale)
        return round(self.ev_threshold + bump, 4)

    def _apply_source_agreement(
        self,
        *,
        p_literal: float,
        p_model_before_sources: float,
        poly_yes: float | None,
        poly_confidence: float | None,
        wallet_alpha_score: float | None,
        wallet_confidence: float | None,
        reason_codes: list[str],
        components: dict,
    ) -> float:
        poly_dir = 0
        wallet_dir = 0

        if (
            poly_yes is not None
            and (poly_confidence or 0.0) >= self.poly_min_confidence
            and abs(poly_yes - p_model_before_sources) >= 0.03
        ):
            poly_dir = 1 if poly_yes > p_model_before_sources else -1
        if (
            wallet_alpha_score is not None
            and (wallet_confidence or 0.0) >= self.wallet_min_confidence
            and abs(wallet_alpha_score) >= 0.05
        ):
            wallet_dir = 1 if wallet_alpha_score > 0 else -1

        if poly_dir != 0 and wallet_dir != 0:
            strength = min(float(poly_confidence or 0.0), float(wallet_confidence or 0.0))
            if poly_dir == wallet_dir:
                shift = self.source_agreement_weight * strength * float(poly_dir)
                p_literal = _clamp_p(p_literal + shift)
                components["source_agreement_shift"] = round(shift, 4)
                if shift > 0:
                    reason_codes.append("SOURCE_AGREE_UP")
                elif shift < 0:
                    reason_codes.append("SOURCE_AGREE_DOWN")
            else:
                # Conflicting external signals: dampen toward pure model probability.
                pull_w = 0.25 * strength
                p_literal = _clamp_p((1.0 - pull_w) * p_literal + pull_w * p_model_before_sources)
                components["source_conflict_pull_weight"] = round(pull_w, 4)
                reason_codes.append("SOURCE_CONFLICT")
        elif poly_dir != 0 or wallet_dir != 0:
            reason_codes.append("SOURCE_SINGLE")

        return round(p_literal, 4)

    # ── Bet sizing constants ──────────────────────────────────────────────
    # Recommended bet size per action card, tiered by signal confidence.
    # High conviction (SCORE_CONF_HIGH)  → $20 (full size)
    # Medium conviction (SCORE_CONF_MED) → $10 (half size)
    # Low conviction  (SCORE_CONF_LOW)   → $5  (minimum — still profitable
    #   for YES per live data but kept small to limit variance)
    _MIN_BET_SIZE: float = 5.0
    _MAX_BET_SIZE: float = 20.0

    @classmethod
    def _size_rec(cls, reason_codes: list[str]) -> float:
        """Return the recommended dollar bet size based on signal confidence tier."""
        if "SCORE_CONF_HIGH" in reason_codes:
            return cls._MAX_BET_SIZE      # $20
        if "SCORE_CONF_MED" in reason_codes:
            return 10.0                    # $10
        return cls._MIN_BET_SIZE          # $5

    @staticmethod
    def _exec_hint(side: str, yes_ask: float, no_ask: float, size_rec: float = 0.0) -> str:
        size_str = f" ${size_rec:.0f}" if size_rec > 0 else ""
        if side == "BUY_YES":
            return f"Buy YES{size_str} at <= {yes_ask:.2f}"
        if side == "BUY_NO":
            return f"Buy NO{size_str} at <= {no_ask:.2f}"
        return ""

    @staticmethod
    def _size_cap(side: str, depth_yes: float, depth_no: float) -> float:
        if side == "BUY_YES":
            return round(depth_yes, 2)
        if side == "BUY_NO":
            return round(depth_no, 2)
        return 0.0

    def _apply_guardrails(
        self,
        side: str,
        *,
        event: EventInfo | None,
        hit_today: bool,
        reason_codes: list[str],
        ev_yes: float,
        yes_ask: float,
        no_ask: float,
    ) -> str:
        if side not in {"BUY_YES", "BUY_NO"}:
            return side

        if side == "BUY_YES" and event and event.speech_state == "scheduled":
            if ev_yes < self.pre_event_yes_threshold:
                reason_codes.append("YES_EV_FILTER")
                return "WATCH"
            if self.block_off_topic_yes and "OFF_TOPIC" in reason_codes and not hit_today:
                reason_codes.append("OFF_TOPIC_GUARD")
                return "WATCH"

        if self.penny_price_threshold > 0 and not hit_today:
            if side == "BUY_YES" and yes_ask <= self.penny_price_threshold:
                has_confirm = ("ON_TOPIC" in reason_codes) or ("POLY_HIGHER" in reason_codes)
                if not has_confirm:
                    reason_codes.append("PENNY_GUARD")
                    return "WATCH"
            if side == "BUY_NO" and no_ask <= self.penny_price_threshold:
                has_confirm = ("OFF_TOPIC" in reason_codes) or ("POLY_LOWER" in reason_codes)
                if not has_confirm:
                    reason_codes.append("PENNY_GUARD")
                    return "WATCH"

        return side

    def _is_material_change(
        self,
        market_id: str,
        side: str,
        ev_yes: float,
        ev_no: float,
        speech_state: str | None,
        now_dt: datetime,
        effective_ev_threshold: float | None = None,
    ) -> bool:
        """Check if this card represents a material change worth notifying about.

        Uses effective_ev_threshold (adaptive, per-market) for EV-crossing detection
        so notifications align with actual gate decisions.  Falls back to self.ev_threshold
        when not provided (e.g. in unit tests).
        """
        prior = self._prior.get(market_id)

        self._prior[market_id] = _PriorCard(
            side=side, ev_yes=ev_yes, ev_no=ev_no,
            speech_state=speech_state, ts=now_dt,
        )

        if prior is None:
            return True

        if side != prior.side:
            return True

        if speech_state != prior.speech_state:
            return True

        thresh = effective_ev_threshold if effective_ev_threshold is not None else self.ev_threshold
        ev_chosen = ev_yes if side == "BUY_YES" else ev_no
        prior_ev = prior.ev_yes if prior.side == "BUY_YES" else prior.ev_no
        ev_was_above = prior_ev >= thresh
        ev_is_above = ev_chosen >= thresh
        if ev_was_above != ev_is_above:
            return True

        if abs(ev_chosen - prior_ev) >= self.material_ev_delta:
            return True

        elapsed = (now_dt - prior.ts).total_seconds()
        if elapsed >= self.cooldown_sec:
            return True

        return False

    def run_once(self) -> int:
        now = utc_now_iso()
        now_dt = datetime.now(tz=timezone.utc)
        today = now[:10]
        self._refresh_market_catalog()
        self._refresh_outcomes_cache()
        self._refresh_event_titles()
        rows = self.conn.execute(
            "SELECT market_id, subject FROM markets ORDER BY market_id"
        ).fetchall()
        snapshot_map = {
            row["market_id"]: row for row in self._latest_snapshots()
        }

        self.event_detector.run_transitions()

        MAX_BUY_PER_EVENT   = 10
        MAX_RISK_PER_EVENT  = 150.0   # max total $ at risk per event (correlated bets)
        # Two-pass scoring: collect all results in Pass 1, then apply
        # EVENT_BET_CAP sorted by EV descending per event in Pass 2.
        # Previously the cap applied in market_id alphabetical order, keeping
        # the first 10 alphabetically rather than the 10 highest-EV bets.
        # Portfolio correlation note: all bets on the same event are correlated.
        # If our event-type/topic assessment is wrong, all bets lose simultaneously.
        # MAX_RISK_PER_EVENT prevents catastrophic correlated drawdowns.
        _pending_payloads: list[tuple[str, float, dict, bool]] = []  # (evt_key, ev, payload, is_material)

        count = 0
        notified = 0
        for market in rows:
            market_id = str(market["market_id"])
            speaker = str(market["subject"])
            market_family_name = self._market_family(market_id)
            series_ticker = self._series_ticker(market_id)
            snap = snapshot_map.get(market_id)
            if snap is None:
                continue

            hit_today, phrase = self._market_hit_today(market_id, today)
            if phrase is None:
                phrase = ""
            known_phrases = [p for p in self.market_phrases.get(market_id, []) if p]
            if not phrase and known_phrases:
                phrase = known_phrases[0]
            # Skip non-phrase contracts in scoring. They are still ingested/snapshotted
            # and surfaced in dashboard coverage diagnostics as untracked.
            if not known_phrases and not phrase:
                continue
            # Skip structurally incompatible series (e.g. duration markets).
            if any(series_ticker.startswith(pfx)
                   for pfx in _UNSCORABLE_SERIES_PREFIXES):
                continue
            event_ticker = self._extract_event_ticker(market_id)
            event = None
            if market_family_name != "windowed":
                event = self.event_detector.get_active_event(speaker, event_ticker)
            if not self._is_market_relevant(event, now_dt, market_id):
                continue

            if market_family_name == "windowed":
                p_literal, reason_codes, components = self._compute_window_p_literal(
                    hit_today, speaker, phrase, now_dt, market_id,
                    event_ticker=event_ticker,
                )
            else:
                p_literal, reason_codes, components = self._compute_p_literal(
                    hit_today, speaker, event, phrase, now_dt, event_ticker
                )
            p_model_before_sources = p_literal

            # When a window market has definitively settled (phrase said = YES,
            # or phrase definitely not said = NO), skip ALL downstream signal
            # modifications.  Poly/wallet/velocity data may be stale or reflect
            # a different time window; blending them in can only hurt accuracy.
            # The WINDOW_SETTLED_YES / WINDOW_SETTLED_NO p_literal (0.96/0.04)
            # is already as accurate as possible — protect it from dilution.
            _window_settled = any(
                c in reason_codes
                for c in ("WINDOW_SETTLED_YES", "WINDOW_SETTLED_NO")
            )

            poly_yes: float | None = None
            poly_confidence: float | None = None
            poly_confidence_bucket: str | None = None
            poly_has_strong_match: bool = False
            wallet_alpha_score: float | None = None
            wallet_confidence: float | None = None
            wallet_bias: str | None = None
            cross_market_edge: float | None = None
            if self.poly_prices and not _window_settled:
                all_phrases = self.market_phrases.get(market_id, [])
                if phrase and phrase not in all_phrases:
                    all_phrases = [phrase] + all_phrases
                timeframe_hint = "monthly" if market_family_name == "windowed" else "event"
                for _pp in all_phrases:
                    poly_signal = self.poly_prices.get_signal(
                        _pp, speaker=speaker, timeframe=timeframe_hint
                    )
                    if poly_signal is None:
                        continue
                    poly_yes = poly_signal.yes_price
                    poly_confidence = round(poly_signal.confidence_score, 4)
                    poly_confidence_bucket = poly_signal.confidence_bucket
                    poly_has_strong_match = poly_signal.has_strong_match
                    components["poly_confidence"] = poly_confidence
                    components["poly_quality"] = round(poly_signal.quality_score, 4)
                    components["poly_confidence_bucket"] = poly_confidence_bucket
                    components["poly_strong_match"] = poly_has_strong_match
                    if poly_signal.book_depth > 0:
                        components["poly_book_depth"] = poly_signal.book_depth
                        components["poly_book_spread"] = poly_signal.book_spread
                    if poly_confidence_bucket == "high":
                        reason_codes.append("POLY_CONF_HIGH")
                    elif poly_confidence_bucket == "medium":
                        reason_codes.append("POLY_CONF_MED")
                    else:
                        reason_codes.append("POLY_CONF_LOW")
                    if not poly_has_strong_match:
                        reason_codes.append("POLY_WEAK_MATCH")
                    # ── Resolution scope guard ────────────────────────────────
                    # A monthly Poly contract (covers whole month) must NOT be
                    # used as-is to price a single-event Kalshi market.  If the
                    # scopes diverge, heavily downgrade confidence so the Poly
                    # price barely moves our estimate.
                    _poly_scope  = poly_signal.timeframe  # "monthly","weekly","event","unknown"
                    _kalshi_scope = timeframe_hint         # "monthly" or "event"
                    _scope_ok = (
                        _poly_scope == "unknown"
                        or _kalshi_scope == "unknown"
                        or _poly_scope == _kalshi_scope
                        or (_poly_scope in ("monthly", "weekly") and _kalshi_scope == "monthly")
                    )
                    if not _scope_ok:
                        poly_confidence = round((poly_confidence or 0.0) * 0.15, 4)
                        reason_codes.append("POLY_SCOPE_MISMATCH")
                        components["poly_scope_mismatch"] = f"{_poly_scope}!={_kalshi_scope}"
                    if poly_yes is not None:
                        break
                # Fallback: if no exact-phrase signal, try speaker-week pool average.
                # This gives a weak calibration anchor for markets not directly
                # on Polymarket (confidence capped at 0.30 so it barely moves p).
                if poly_yes is None:
                    pool_sig = self.poly_prices.get_pool_signal(
                        speaker=speaker,
                        timeframe=timeframe_hint,
                    )
                    if pool_sig is not None:
                        poly_yes = pool_sig.yes_price
                        poly_confidence = pool_sig.confidence_score
                        poly_confidence_bucket = "low"
                        poly_has_strong_match = False
                        components["poly_pool_signal"] = True
                        reason_codes.append("POLY_POOL")

                if poly_yes is not None:
                    model_before_poly = p_literal
                    components["poly_yes"] = round(poly_yes, 4)

                    should_blend = (
                        (poly_confidence or 0.0) >= self.poly_min_confidence
                        and poly_has_strong_match
                    )
                    if should_blend:
                        blend_w = self.poly_blend_weight * float(poly_confidence or 0.0)
                        if poly_has_strong_match and (poly_confidence or 0) >= 0.80:
                            blend_w *= 1.5
                        blend_w = min(blend_w, 0.50)
                        p_blended = (1 - blend_w) * p_literal + blend_w * poly_yes
                        p_blended = max(P_MIN, min(P_MAX, p_blended))
                        components["poly_weight_effective"] = round(blend_w, 4)
                        components["p_model"] = round(model_before_poly, 4)
                        components["p_blended"] = round(p_blended, 4)
                        p_literal = round(p_blended, 4)
                    elif (poly_confidence or 0.0) < self.poly_min_confidence:
                        reason_codes.append("POLY_LOW_CONF")
                    else:
                        reason_codes.append("POLY_WEAK_MATCH_SKIP")

                    if abs(poly_yes - model_before_poly) >= 0.15:
                        reason_codes.append("POLY_DIVERGE")
                    if poly_yes > model_before_poly + 0.05:
                        # POLY_HIGHER: Polymarket prices YES more bullishly than our
                        # model.  Live data shows 30% WR on BUY_NO bets tagged
                        # POLY_HIGHER (-$7.95) — Poly smart money betting YES is
                        # consistently right.  Veto BUY_NO when poly is clearly
                        # higher (>10¢ gap) with at least medium confidence.
                        reason_codes.append("POLY_HIGHER")
                    elif poly_yes < model_before_poly - 0.05:
                        reason_codes.append("POLY_LOWER")

                    # cross_market_edge calculated after snapshot values are available (below)

            # Live settlement signal — use same-event settlements as context
            if self.live_settlements and event_ticker:
                settlement_sig = self.live_settlements.get_signal(
                    event_ticker=event_ticker,
                    phrase=phrase,
                    speaker=speaker,
                )
                if settlement_sig is not None and settlement_sig.event_is_active:
                    components["settlement_yes_count"] = settlement_sig.yes_count
                    components["settlement_no_count"] = settlement_sig.no_count
                    components["settlement_age_min"] = round(
                        settlement_sig.latest_settlement_age_minutes, 1
                    )
                    if settlement_sig.phrase_settled_yes:
                        # This phrase already settled YES — market should be ~0.95
                        p_literal = max(p_literal, 0.90)
                        reason_codes.append("SETTLED_YES")
                    elif settlement_sig.phrase_settled_no:
                        # This phrase already settled NO — market should be ~0.05
                        p_literal = min(p_literal, 0.10)
                        reason_codes.append("SETTLED_NO")
                    elif settlement_sig.p_adjustment != 0.0:
                        # Event is active + co-occurrence boosts
                        adjusted = p_literal + settlement_sig.p_adjustment
                        p_literal = round(max(P_MIN, min(P_MAX, adjusted)), 4)
                        if settlement_sig.cooccurrence_boosts:
                            best = max(
                                settlement_sig.cooccurrence_boosts,
                                key=lambda x: x.p_boost,
                            )
                            components["cooccur_trigger"] = best.trigger_phrase
                            components["cooccur_rate"] = best.rate
                            reason_codes.append("COOCCUR_BOOST")
                        else:
                            reason_codes.append("EVENT_ACTIVE")

            if self.wallet_signals and not _window_settled:
                timeframe_hint = "monthly" if market_family_name == "windowed" else "event"
                wallet_signal = self.wallet_signals.get_signal(
                    phrase, speaker=speaker, timeframe=timeframe_hint
                )
                if wallet_signal is not None:
                    wallet_alpha_score = round(wallet_signal.wallet_alpha_score, 4)
                    wallet_confidence = round(wallet_signal.confidence, 4)
                    wallet_bias = wallet_signal.smart_flow_bias
                    components["wallet_alpha_score"] = wallet_alpha_score
                    components["wallet_confidence"] = wallet_confidence
                    components["wallet_bias"] = wallet_bias
                    components["wallet_extreme_bets"] = wallet_signal.extreme_bet_count
                    components["wallet_avg_price"] = round(wallet_signal.avg_trade_price, 4)

                    rep_alpha = wallet_signal.reputation_weighted_alpha
                    if abs(rep_alpha) > 0.01:
                        components["wallet_reputation_alpha"] = round(rep_alpha, 4)

                    if wallet_confidence >= self.wallet_min_confidence:
                        alpha = wallet_alpha_score
                        if abs(rep_alpha) > 0.02:
                            alpha = 0.6 * alpha + 0.4 * rep_alpha
                        shift = self.wallet_flow_weight * wallet_confidence * alpha
                        if wallet_signal.extreme_bet_count >= 2:
                            shift *= 1.3
                        shift = max(-0.15, min(0.15, shift))
                        components["wallet_shift"] = round(shift, 4)
                        p_literal = round(_clamp_p(p_literal + shift), 4)
                        if shift > 0:
                            reason_codes.append("WALLET_FLOW_UP")
                        elif shift < 0:
                            reason_codes.append("WALLET_FLOW_DOWN")
                        if wallet_signal.extreme_bet_count >= 2:
                            reason_codes.append("WALLET_EXTREME_BETS")
                        if abs(rep_alpha) > 0.05:
                            reason_codes.append("WALLET_REP_STRONG")
                    else:
                        reason_codes.append("WALLET_LOW_CONF")

            if not _window_settled:
                p_literal = self._apply_source_agreement(
                    p_literal=p_literal,
                    p_model_before_sources=p_model_before_sources,
                    poly_yes=poly_yes,
                    poly_confidence=poly_confidence,
                    wallet_alpha_score=wallet_alpha_score,
                    wallet_confidence=wallet_confidence,
                    reason_codes=reason_codes,
                    components=components,
                )

            # ── Price velocity (smart money) signal ───────────────────────────
            # If the market's YES price is moving unusually fast, that signals
            # informed flow. Adjust p_literal in the direction of the move.
            # Signal quality improves with continuous runner operation (7+ days).
            # Skip for settled window phrases — velocity data may predate settlement.
            if self.price_velocity and not _window_settled:
                vel_sig = self.price_velocity.get(market_id)
                if vel_sig is not None:
                    vel_adj = vel_sig.p_adjustment()
                    if vel_adj != 0.0:
                        components["velocity_signal"]   = vel_sig.signal
                        components["velocity_strength"] = vel_sig.signal_strength
                        components["velocity_window"]   = vel_sig.signal_window
                        components["velocity_adj"]      = vel_adj
                        p_literal = round(_clamp_p(p_literal + vel_adj), 4)
                        reason_codes.append(vel_sig.reason_tag)

            yes_ask = float(snap["yes_ask"])
            no_ask = float(snap["no_ask"])
            _snap_keys = snap.keys() if hasattr(snap, "keys") else []
            yes_bid = float(snap["yes_bid"]) if "yes_bid" in _snap_keys else 0.0
            no_bid  = float(snap["no_bid"])  if "no_bid"  in _snap_keys else 0.0
            depth_yes = float(snap["depth_yes"])
            depth_no = float(snap["depth_no"])
            spread = float(snap["spread"])  # kept for logging / payload only
            # Side-appropriate bid-ask spread:
            # For BUY_YES we care about the YES spread; for BUY_NO we care about
            # the NO spread.  Using the YES spread for all bets was incorrectly
            # blocking BUY_NO on polarised markets (YES at 80% → wide YES spread,
            # but the NO side can still be very tight and liquid).
            yes_spread = round(yes_ask - yes_bid, 4) if yes_ask > yes_bid > 0 else spread
            no_spread  = round(no_ask  - no_bid,  4) if no_ask  > no_bid  > 0 else spread

            if poly_yes is not None and poly_has_strong_match and (poly_confidence or 0) >= 0.70:
                cross_market_edge = _compute_cross_market_edge(
                    poly_yes=poly_yes,
                    kalshi_yes=yes_ask,
                )
                if cross_market_edge is not None:
                    components["cross_market_edge"] = round(cross_market_edge, 4)
                    if cross_market_edge >= 0.10:
                        reason_codes.append("CROSS_MARKET_ARB")
                    elif cross_market_edge >= 0.05:
                        reason_codes.append("CROSS_MARKET_EDGE")

            market_implied = yes_ask
            # Record raw (uncalibrated) values for inspection
            components["p_raw"]          = round(p_literal, 4)
            components["p_pre_calibration"] = round(p_literal, 4)

            # ── Platt scaling recalibration ───────────────────────────────────
            # Corrects systematic bias in the raw model (e.g. model=0.6 actual=0.91).
            # The calibrated p is used for all EV and gate calculations; raw p
            # is kept in components for debugging.
            # Use stratified calibrator: "live" during events, "pre_event" before,
            # "default" (pooled) for window markets and unknown contexts.
            if self.calibrator is not None and self.calibrator.is_fitted:
                # Event-type specific stratification (Session 65 - highest priority)
                _speaker_key = (speaker or "").lower().strip()
                _event_type = (event.event_type if event else "general")
                _is_trump_spk = _speaker_key == "trump"
                _is_sports = _speaker_key in ("nba", "ncaab", "mlb", "mma")
                
                # Try event-type specific strata first
                if _is_sports and _event_type == "nba_broadcast":
                    _calib_key = "sports_broadcast"
                elif _is_sports and _event_type in ("other", "general"):
                    _calib_key = "sports_other"  
                elif _event_type == "briefing" and _speaker_key == "leavitt":
                    _calib_key = "briefing"
                elif _event_type == "announcement":
                    _calib_key = "announcement" 
                # Fall back to speaker × timing strata
                elif "LIVE" in reason_codes:
                    _calib_key = "live_trump" if _is_trump_spk else "live_non_trump"
                    if not self.calibrator.has_stratum(_calib_key):
                        _calib_key = "live"
                elif "PRE_EVENT" in reason_codes:
                    _calib_key = "pre_event_trump" if _is_trump_spk else "pre_event_non_trump"
                    if not self.calibrator.has_stratum(_calib_key):
                        _calib_key = "pre_event"
                else:
                    _calib_key = "default"
                
                # Fallback chain: if event-type stratum not available, use speaker×timing
                if not self.calibrator.has_stratum(_calib_key):
                    if "LIVE" in reason_codes:
                        _calib_key = "live_trump" if _is_trump_spk else "live_non_trump"
                        if not self.calibrator.has_stratum(_calib_key):
                            _calib_key = "live"
                    elif "PRE_EVENT" in reason_codes:
                        _calib_key = "pre_event_trump" if _is_trump_spk else "pre_event_non_trump" 
                        if not self.calibrator.has_stratum(_calib_key):
                            _calib_key = "pre_event"
                    else:
                        _calib_key = "default"
                        
                p_calibrated = self.calibrator.calibrate(p_literal, key=_calib_key, speaker=speaker or "")
                if abs(p_calibrated - p_literal) >= 0.01:
                    components["p_calibrated"]  = round(p_calibrated, 4)
                    components["calib_shift"]   = round(p_calibrated - p_literal, 4)
                    components["calib_key"]     = _calib_key
                    reason_codes.append("PLATT_CALIBRATED")
            else:
                p_calibrated = p_literal

            market_divergence = market_implied - p_calibrated  # positive = market more bullish
            components["market_implied"]   = round(market_implied, 4)
            components["market_divergence"] = round(market_divergence, 4)

            # KL divergence: information-theoretic edge quality measure
            kl_div = kl_divergence(p_calibrated, market_implied)
            components["kl_divergence"] = round(kl_div, 4)
            if kl_div >= 0.15:
                reason_codes.append("KL_HIGH")
            elif kl_div >= 0.05:
                reason_codes.append("KL_MED")

            ev_yes = round(p_calibrated - yes_ask, 4)
            ev_no = round((1.0 - p_calibrated) - no_ask, 4)
            score_confidence = self._compute_score_confidence(
                spread=spread,
                depth_yes=depth_yes,
                depth_no=depth_no,
                poly_confidence=poly_confidence,
                wallet_confidence=wallet_confidence,
                reason_codes=reason_codes,
                p_literal=p_literal,
                yes_ask=yes_ask,
            )
            if score_confidence >= 0.75:
                reason_codes.append("SCORE_CONF_HIGH")
            elif score_confidence >= 0.50:
                reason_codes.append("SCORE_CONF_MED")
            else:
                reason_codes.append("SCORE_CONF_LOW")

            # ── Confidence-based early exit (Session 65 P3.2) ──────────────
            # Low-confidence bets without any high-signal reason codes are
            # processed through 17+ gates but almost never produce edge.
            # Exit early to reduce noise and focus on high-conviction cases.
            # HIGH-SIGNAL reasons that override early exit:
            #   BIAS_MAP_RATE      — empirical series-specific rate (strongest)
            #   PHRASE_HIT         — confirmed in live transcript (near-certain)
            #   CAUSAL_OVERRIDE    — structurally guaranteed phrase
            #   SCORE_CONF_HIGH    — high divergence/liquidity confidence
            _high_signal_reasons = {
                "BIAS_MAP_RATE", "PHRASE_HIT", "CAUSAL_OVERRIDE", "SCORE_CONF_HIGH",
                "BIAS_MAP_UNDERPRICED", "BIAS_MAP_OVERPRICED",
                "KL_HIGH",   # high model-market divergence = real edge
                "LLM_SUPPRESS_HIGH",  # strong LLM signal should not be early-exited
                "ROLLING_N5", "ROLLING_N3",   # rolling rate signal = has history
                "SERIES_HISTORY",    # known historical outcomes for this series
            }
            _has_high_signal = bool(_high_signal_reasons & set(reason_codes))
            # Also skip early exit for well-known speakers with thick outcome history
            # (trump, leavitt, mamdani) — their scoring is calibrated even at MED conf
            _well_known_speaker = (speaker or "").lower().strip() in (
                "trump", "leavitt", "mamdani", "powell"
            )
            _early_exit = score_confidence < 0.40 and not _has_high_signal and not _well_known_speaker
            if _early_exit:
                reason_codes.append("LOW_CONF_EARLY_EXIT")

            effective_ev_threshold = self._adaptive_ev_threshold(score_confidence)
            if effective_ev_threshold > self.ev_threshold + 0.01:
                reason_codes.append("ADAPTIVE_THRESHOLD_RAISED")

            # risk_adjusted_ev is kept for display/logging but NOT used for the
            # betting decision.  Multiplying EV by score_confidence AND raising the
            # threshold (adaptive_ev_threshold) creates a double-penalty that blocks
            # legitimate trades with ev=0.13–0.20 when confidence is medium/low.
            # The adaptive threshold alone already penalises low-confidence data.
            risk_adjusted_ev_yes = round(ev_yes * score_confidence, 4)
            risk_adjusted_ev_no = round(ev_no * score_confidence, 4)
            components["score_confidence"] = score_confidence
            components["effective_ev_threshold"] = effective_ev_threshold
            components["risk_adjusted_ev_yes"] = risk_adjusted_ev_yes
            components["risk_adjusted_ev_no"] = risk_adjusted_ev_no

            # ── Bias map: empirically confirmed overpriced phrase ─────────
            # Tag phrase if bias map flags it as overpriced, but do NOT relax
            # any gates.  Live data (153 bets) shows BIAS_MAP_OVERPRICED bets
            # have only 14.3% WR (-$2.79 P&L) — the map is stale and its
            # gate-bypass logic costs more than it gains.  Keep the tag for
            # monitoring/auditing but remove all gate exceptions until the
            # bias map is rebuilt from recent data.
            _bias_overpriced = (
                self.bias_map is not None
                and self.bias_map.is_overpriced(series_ticker, phrase, threshold=0.20)
            )
            if _bias_overpriced:
                reason_codes.append("BIAS_MAP_OVERPRICED")

            # Structural underpricing: bias map has N>=15 outcomes showing
            # the market chronically prices YES 30+ cents below empirical rate.
            # These phrases are NOT rare — the market is just wrong about them.
            # Flag bypasses YES_PRICE_FLOOR_BLOCK and SETTLED_MARKET_BLOCK below.
            _bias_underpriced = (
                self.bias_map is not None
                and self.bias_map.is_underpriced(series_ticker, phrase,
                                                  threshold=0.30, min_n=15)
            )
            if _bias_underpriced:
                reason_codes.append("BIAS_MAP_UNDERPRICED")
                # Empirical p-floor: override LLM suppression when the bias map
                # has strong historical evidence (N>=15, gap>=30¢). The LLM
                # suppresses based on current event context; for rolling daily/weekly
                # "say" markets the empirical window rate is the better signal.
                # Apply: p_floor = emp_rate * time_decay * news_pressure
                _bm_entry = self.bias_map.get(series_ticker, phrase)
                if _bm_entry is not None and no_ask < 0.98:
                    # Skip p-floor when market has resolved NO (no_ask >= 0.97).
                    # A resolved-NO market at 1-2¢ is correct — the phrase was not said.
                    _td   = components.get("time_decay") or 1.0
                    _news = components.get("news_pressure") or 1.0
                    _p_floor = _clamp_p(_bm_entry.empirical_rate * _td * _news)
                    if p_calibrated < _p_floor:
                        reason_codes.append("P_FLOOR_APPLIED")
                        p_calibrated = _p_floor
                        p_literal    = _p_floor   # keep display consistent
                        ev_yes = round(p_calibrated - yes_ask, 4)
                        ev_no  = round((1.0 - p_calibrated) - no_ask, 4)

            # BUY_NO requires NO_EV_PREMIUM (1.5×) more EV than BUY_YES.
            # Exception: bias map overpriced phrases get reduced premium (1.0x)
            # since we have empirical evidence the market systematically misprices.
            _effective_no_premium = 1.0 if _bias_overpriced else NO_EV_PREMIUM

            # ── Pre-event vs live strategy split (Session 65 P3.1) ─────────
            # Pre-event bets use only historical base rates with no live signal
            # to confirm direction — use a higher EV threshold.
            # Live bets have time-decay confirmation — use standard threshold.
            # If early exit was flagged, skip directly to WATCH.
            _is_pre_event = "PRE_EVENT" in reason_codes
            _effective_ev_yes_threshold = (
                PRE_EVENT_EV_THRESHOLD if _is_pre_event
                else effective_ev_threshold
            )
            _effective_ev_no_threshold = (
                PRE_EVENT_EV_THRESHOLD * _effective_no_premium if _is_pre_event
                else effective_ev_threshold * _effective_no_premium
            )

            if _early_exit:
                tentative_side = "WATCH"
            elif ev_yes >= _effective_ev_yes_threshold and ev_yes >= ev_no:
                tentative_side = "BUY_YES"
            elif ev_no >= _effective_ev_no_threshold:
                tentative_side = "BUY_NO"
            else:
                tentative_side = "WATCH"

            # ── GLOBAL BUY_YES BLOCK ──────────────────────────────────────────
            # Live data (138 bets): BUY_YES → 33.3% WR at EVERY EV threshold.
            # The model systematically overestimates phrase probability and buys
            # YES at prices that are already fair. BUY_NO is where all edge lives
            # (52.8% WR, profitable at high-EV). Block all BUY_YES until the model
            # demonstrates >45% WR on a 50+ bet sample.
            if tentative_side == "BUY_YES":
                reason_codes.append("GLOBAL_YES_BLOCK")
                tentative_side = "WATCH"

            if _is_pre_event and tentative_side == "WATCH" and (
                ev_yes >= effective_ev_threshold or ev_no >= effective_ev_threshold * _effective_no_premium
            ):
                # Would have passed standard threshold but not pre-event threshold
                reason_codes.append("PRE_EVENT_EV_FILTER")

            # ── Already-settled market block ──────────────────────────────
            # When yes_ask >= 0.85 the phrase has effectively already been said
            # (or is near-certain YES) — no point betting NO.
            # Likewise no_ask >= 0.85 means near-certain NO — no point betting YES.
            # Threshold lowered from 0.95 to 0.85 after dry-run revealed 39 BUY_NO
            # cards being generated against 80-94¢ markets on ended events.
            # The 0.95 threshold missed the 0.80-0.94 band where the market is
            # effectively settled but not technically at the old ceiling.
            if tentative_side == "BUY_NO" and yes_ask >= 0.85:
                reason_codes.append("SETTLED_MARKET_BLOCK")
                tentative_side = "WATCH"
            elif tentative_side == "BUY_YES" and no_ask >= 0.85:
                # BIAS_MAP_UNDERPRICED exception: when the bias map shows 30+ cents
                # structural underpricing (N>=15 outcomes), no_ask >= 0.85 can mean
                # the market is chronically wrong (not that the event has settled).
                # BUT if no_ask >= 0.97, the market has almost certainly RESOLVED NO
                # (e.g., an ended NCAAB game) — never bypass resolution signals.
                _settled_exception = _bias_underpriced and no_ask < 0.98
                if not _settled_exception:
                    reason_codes.append("SETTLED_MARKET_BLOCK")
                    tentative_side = "WATCH"

            # ── Event-ended + high-yes block ──────────────────────────────
            # When an event has already ended AND the market prices YES >= 0.70,
            # the market has effectively resolved or the phrase was said.
            # Our model's EVENT_ENDED p_literal=0.02 creates artificially high
            # EV_NO, but we have no ability to verify the phrase wasn't said —
            # the event is already over and the market reflects settlement.
            if (tentative_side == "BUY_NO"
                    and "EVENT_ENDED" in reason_codes
                    and yes_ask >= 0.70):
                reason_codes.append("ENDED_EVENT_BULLISH_BLOCK")
                tentative_side = "WATCH"

            # ── Cheap NO block ─────────────────────────────────────────────
            # When no_ask < 0.40, the market thinks YES is likely (yes_ask > 0.60).
            # Live data: BUY_NO with no_ask < 0.40 → 34% WR overall, 0% WR on
            # Trump, 22% WR on MLB. The market is almost always right when it
            # prices YES this high. Don't fight it.
            if tentative_side == "BUY_NO" and no_ask < 0.40:
                reason_codes.append("CHEAP_NO_BLOCK")
                tentative_side = "WATCH"

            # ── Expensive NO block ────────────────────────────────────────────
            # When no_ask > 0.55, we're paying too much for NO. Even with 65% WR,
            # paying $0.60-0.80 per contract means losses are catastrophic.
            # Live data: no_ask 0.40-0.50 → +$5.54 PnL; no_ask 0.60+ → -$9.57.
            # The asymmetric payoff kills us: win $0.30 but lose $0.70.
            if tentative_side == "BUY_NO" and no_ask > 0.55:
                reason_codes.append("EXPENSIVE_NO_BLOCK")
                tentative_side = "WATCH"

            # ── Conviction floor for BUY_NO ────────────────────────────────
            _conviction_threshold = 0.35 if _bias_overpriced else 0.20
            if tentative_side == "BUY_NO" and p_calibrated >= _conviction_threshold:
                reason_codes.append("NO_CONVICTION_FLOOR")
                tentative_side = "WATCH"

            # ── Weekly Trump-say window BUY_NO block ─────────────────────
            # KXTRUMPSAY / KXTRUMPSAYEP weekly contracts resolve if Trump says
            # a phrase ANYWHERE in 7 days (all speeches, tweets, statements).
            # For any phrase with p_per_event ≥ 10%, the 7-day compound prob
            # is 1-(0.9)^7 = 52%+, making BUY_NO a bad bet.  Live data shows
            # KXTRUMPSAY-26MAR23 had 23 BUY_NO bets with 0% win rate.
            # Block BUY_NO on these series unless p_calibrated < 0.08
            # (phrase almost never used, e.g. very rare Trump-specific jargon).
            _series = series_ticker or ""
            if (tentative_side == "BUY_NO"
                    and _series in ("KXTRUMPSAY", "KXTRUMPSAYEP")
                    and p_calibrated >= 0.08):
                reason_codes.append("WEEKLY_WINDOW_NO_BLOCK")
                tentative_side = "WATCH"


            # ── Pre-event BUY_NO block ────────────────────────────────────
            # Pre-event bets (before speech starts) show 17% WR on BUY_NO
            # in live data vs 53% WR for LIVE bets.  Pre-event, we are
            # essentially betting on absence of a phrase using only base
            # rates — there is no live evidence of what has or hasn't been
            # said.  Only allow pre-event BUY_NO when p_calibrated < 0.15
            # (extreme NO conviction — phrase is very rare for this speaker).
            if (tentative_side == "BUY_NO"
                    and "PRE_EVENT" in reason_codes
                    and p_calibrated >= 0.15):
                reason_codes.append("PRE_EVENT_NO_BLOCK")
                tentative_side = "WATCH"

            # ── Market-bullish block for BUY_NO ───────────────────────────
            # Lowered from 0.70 → 0.65: live data (153 bets) shows yes_ask 0.7-0.8
            # has 8-37% WR on BUY_NO — market consensus at 65¢+ is reliable.
            # BIAS_MAP_OVERPRICED exception removed: live data shows 14.3% WR
            # on BIAS_MAP_OVERPRICED bets — the bias map is stale and its
            # override of market consensus is costing money.
            if (tentative_side == "BUY_NO"
                    and yes_ask >= 0.65
                    and p_calibrated >= 0.20):
                reason_codes.append("MARKET_BULLISH_BLOCK")
                tentative_side = "WATCH"

            # ── Price-range gates (empirically derived from live data) ───────────
            # Live outcome analysis (207 bets) shows two profitable price zones:
            #   BUY_NO:  YES_ASK < 0.42  → 80% WR, +$1.34   (market overprices YES)
            #   BUY_YES: YES_ASK 0.28–0.54 → 53% WR, +$2.90 (market underprices YES)
            #
            # Outside these zones performance collapses:
            #   BUY_NO  YES_ASK 0.42–0.60: 27% WR, -$9.01   ← #1 money loser
            #   BUY_NO  YES_ASK >0.75:      5% WR, -$5.67   ← market is right
            #   BUY_YES YES_ASK <0.28:     10% WR, -$0.82   ← market is right
            #   BUY_YES YES_ASK 0.54–0.70: 25% WR, -$4.13   ← model overestimates buzz
            #
            # These are hard absolute gates, checked BEFORE Poly/Kelly/spread gates.

            # BUY_NO price ceiling (speaker-calibrated).
            #
            # Trump: market is highly efficient — his base rates are well-established
            # and market prices YES accurately.
            # Live outcome analysis by price band:
            #   <35¢ YES:  4 bets, 100% WR, +$1.19  ← profitable sweet spot
            #   35–42¢ YES: 6 bets,  33% WR, −$2.03  ← losing even at SCORE_CONF_HIGH
            #   42–50¢ YES: 5 bets,  40% WR, −$1.28  ← old ceiling, also losing
            #   50¢+ YES:  12 bets,  17% WR, −$2.19  ← market is right
            # Ceiling lowered 0.42 → 0.35 to cut the 35-42¢ band which is net-negative.
            #
            # Non-Trump speakers (Mamdani, etc.): Kalshi prices
            # their markets using Trump-calibrated base rates, which systematically
            # overprices Trump-specific phrases for non-Trump principals.
            # "defense" at 91¢ for a British PM is almost always wrong — the market
            # maker copy-pasted Trump rates.  BUY_NO at 60-75¢ for these speakers
            # wins 60-67% of the time (live data).  Allow up to 75¢ for thin speakers.
            _is_trump = (speaker or "").lower().strip() == "trump"
            _no_ceiling = 0.35 if _is_trump else 0.75
            # Signing/scripted event exception: for focused ceremonial events (signing,
            # remarks, announcement) where topic is known, many phrases are structurally
            # off-topic. Allow Trump BUY_NO up to 0.55 when we have p_floor or
            # event_llm evidence the phrase is off-topic (OFF_TOPIC or EVENT_LLM_SUPPRESS).
            _is_signing_event = (
                event is not None
                and event.event_type in ("signing", "announcement", "remarks")
            )
            if _is_trump and _is_signing_event and (
                "OFF_TOPIC" in reason_codes or "EVENT_LLM_SUPPRESS" in reason_codes
            ):
                _no_ceiling = 0.55   # allow higher-priced NO bets for off-topic signing phrases
            # Bias map overpriced exception: when empirical data shows systematic
            # overpricing (≥20¢ gap, N≥10), allow BUY_NO even above normal ceiling.
            _price_range_exception = _bias_overpriced
            if (tentative_side == "BUY_NO" 
                    and yes_ask > _no_ceiling 
                    and not _price_range_exception):
                reason_codes.append("NO_PRICE_RANGE_BLOCK")
                tentative_side = "WATCH"

            # BUY_YES price floor: don't bet YES when market prices it < 35¢.
            # Markets below 35¢ reflect phrases that rarely get said.  
            # Raised from 28¢ → 32¢ → 35¢: live data (30d) shows 25-35¢ range
            # has only 30% WR (-$0.23 total on 20 bets).  35-45¢ range shows  
            # 55.6% WR (+$1.50 on 9 bets) — the 35¢ cut preserves profitable
            # bets while eliminating the systematic loser bucket.
            # BUY_YES ask floor: 35¢ globally.
            # Data: <35¢ has 33% WR, 35-45¢ has 55.6% WR.
            # BIAS_MAP_RATE exception: structural certainties (arena names, event p_overrides)
            # can be valid YES bets below 35¢ — they have empirical backing.
            _yes_floor = LIVE_BUY_YES_ASK_FLOOR   # 0.35 globally
            _floor_exception = (
                (_bias_underpriced and no_ask < 0.98)
                or "BIAS_MAP_RATE" in reason_codes  # structural certainty
                or "P_FLOOR_APPLIED" in reason_codes  # empirical override
            )
            if tentative_side == "BUY_YES" and yes_ask < _yes_floor and not _floor_exception:
                reason_codes.append("YES_PRICE_FLOOR_BLOCK")
                tentative_side = "WATCH"

            # BUY_YES price ceiling: don't chase YES when market already prices
            # it at 54%+. Above 54¢, the crowd has priced the signal; we add
            # noise, not edge (25% WR, -$4.13 in live data; LLM-p_floor bets at
            # >54¢ showed 29% WR, -$4.88 — exception not justified by data).
            # When fresh Truth Social p_floors prove out in live testing, re-add
            # the CAUSAL_OVERRIDE / P_FLOOR_APPLIED exception here.
            # Exempt PHRASE_HIT (p=0.98): phrase was already confirmed in transcript;
            # market price irrelevant since result is near-certain.
            if tentative_side == "BUY_YES" and yes_ask > 0.54 and not hit_today:
                reason_codes.append("YES_PRICE_CEILING_BLOCK")
                tentative_side = "WATCH"

            # ── Market-bearish block for BUY_YES ───────────────────────────
            # (Legacy micro-market guard — subsumed by YES_PRICE_FLOOR_BLOCK above
            # but kept as an explicit label for very-thin <6¢ markets.)
            if tentative_side == "BUY_YES" and yes_ask < 0.06:
                # Same resolution guard: don't bypass for resolved-NO markets.
                _bearish_exception = _bias_underpriced and no_ask < 0.98
                if not _bearish_exception:
                    reason_codes.append("MARKET_BEARISH_BLOCK")
                    tentative_side = "WATCH"

            # ── Market-disagree block for BUY_NO ─────────────────────────
            # When the market prices YES significantly higher than our model
            # predicts (yes_ask > p_calibrated + 0.25) AND p >= 0.20, the
            # model lacks strong NO conviction to justify the disagreement.
            # If p < 0.20, the model has strong NO conviction and the
            # divergence may be legitimate edge (e.g. rare phrase, no event).
            if (tentative_side == "BUY_NO"
                    and p_calibrated >= 0.20
                    and yes_ask > p_calibrated + 0.25):
                reason_codes.append("MARKET_DISAGREE_NO")
                tentative_side = "WATCH"

            # ── Wallet low-confidence block (unconditional) ───────────────
            # When the wallet signal is present but below minimum confidence,
            # WALLET_LOW_CONF is tagged.  Live data (394 outcomes analysis):
            # WALLET_LOW_CONF BUY_NO: 25% WR (-$1.77 on 12 bets).
            # WALLET_LOW_CONF BUY_YES: 31.6% WR (-$1.38 on 12 bets).
            # Made unconditional: low-confidence wallet flow is pure noise
            # regardless of what other signals are present.  The "strong override"
            # exception was reverted after live data showed it didn't help —
            # even strong signals don't rescue low-conf wallet reads.
            if (tentative_side in ("BUY_YES", "BUY_NO")
                    and "WALLET_LOW_CONF" in reason_codes):
                reason_codes.append("WALLET_LOW_CONF_BLOCK")
                tentative_side = "WATCH"

            # ── Poly VETO for BUY_NO ─────────────────────────────────────
            # When Polymarket has a DIRECT, confident match and prices YES
            # at least 10¢ above Kalshi's ask, the independent crowd is
            # bullish while we are saying NO.  Backtesting shows this pattern
            # costs ~$9 per 100 bets.  Only apply for strong, confident poly
            # matches — pool/proxy signals are too noisy to trust for vetoing.
            if (tentative_side == "BUY_NO"
                    and poly_yes is not None
                    and poly_has_strong_match
                    and (poly_confidence or 0) >= 0.60
                    and poly_yes > yes_ask + 0.10):
                reason_codes.append("POLY_VETO_NO")
                tentative_side = "WATCH"

            # ── CROSS_MARKET_ARB veto for BUY_NO ─────────────────────────
            # When Poly prices YES 10+ cents ABOVE Kalshi (CROSS_MARKET_ARB),
            # betting NO means fighting BOTH Kalshi AND Polymarket.
            # Live data (4 bets): 0% WR, -$1.28. Block unconditionally.
            if (tentative_side == "BUY_NO"
                    and "CROSS_MARKET_ARB" in reason_codes):
                reason_codes.append("CROSS_MARKET_ARB_BLOCK")
                tentative_side = "WATCH"

            # ── POLY_HIGHER veto for BUY_NO ──────────────────────────────
            # Broader than POLY_VETO_NO: even without a strong match, when
            # Polymarket consistently prices this phrase's YES at least 15¢
            # above our model AND at least low confidence, Poly smart money
            # is right.  Live data: POLY_HIGHER tag → 30% WR (-$7.95) on
            # BUY_NO.  POLY_CONF_HIGH → 14.3% WR (-$2.98).  These are the
            # two worst-performing tags in the entire signal stack.
            # Threshold lowered from 0.40 to 0.25: low-conf Poly still costs
            # -$0.024/bet × 34 bets = -$0.81; med+high were already blocked.
            if (tentative_side == "BUY_NO"
                    and poly_yes is not None
                    and "POLY_HIGHER" in reason_codes
                    and (poly_confidence or 0) >= 0.25
                    and poly_yes > model_before_poly + 0.15):
                reason_codes.append("POLY_HIGHER_VETO")
                tentative_side = "WATCH"

            # ── POLY_HIGHER block for BUY_YES ────────────────────────────
            # When Poly already prices YES higher than Kalshi (POLY_HIGHER),
            # our BUY_YES edge is gone — the crowd has already priced it.
            # We'd be buying at market where smart money has already moved.
            # Session 65 P4.1: POLY_HIGHER + BUY_YES → 21.4% WR, -$1.96
            # on 14 bets. When Poly is bullish, don't chase the move.
            if (tentative_side == "BUY_YES"
                    and "POLY_HIGHER" in reason_codes):
                reason_codes.append("POLY_HIGHER_YES_BLOCK")
                tentative_side = "WATCH"

            # ── ROLLING_N5 + Poly disagreement veto for BUY_NO ──────────
            # When our rolling rate has compressed p_literal downward (ROLLING_N5)
            # AND Polymarket simultaneously signals YES is likely (POLY_HIGHER or
            # POLY_DIVERGE), the rolling compression is likely stale or wrong.
            # Poly has fresher/better information about the phrase's likelihood
            # in the current context.
            # Live data: ROLLING_N5+POLY BUY_NO → 66% phrase-said rate, -$4.95
            # (-$0.171/bet across 29 bets).  ROLLING_N5 without Poly is profitable
            # (+$0.067/bet on 9 bets) so the gate is the COMBINATION, not rolling alone.
            if (tentative_side == "BUY_NO"
                    and "ROLLING_N5" in reason_codes
                    and ("POLY_HIGHER" in reason_codes or "POLY_DIVERGE" in reason_codes)):
                reason_codes.append("ROLLING_POLY_CONFLICT")
                tentative_side = "WATCH"

            # ── Low/medium confidence BUY_NO block ───────────────────────
            # Live data (243 bets): SCORE_CONF_HIGH BUY_NO → +$0.95 (+$2.24
            # combined with YES); MED BUY_NO → -$2.88; LOW BUY_NO → -$2.17.
            # Only HIGH-confidence BUY_NO bets are net-positive. BUY_YES is
            # profitable at all confidence levels — this gate is NO-only.
            if (tentative_side == "BUY_NO"
                    and "SCORE_CONF_HIGH" not in reason_codes):
                reason_codes.append("LOW_CONF_NO_BLOCK")
                tentative_side = "WATCH"

            # ── OFF_TOPIC BUY_NO block ────────────────────────────────────
            # Live data (11 bets): OFF_TOPIC BUY_NO → -$2.35 P&L.
            # Off-topic means the phrase is contextually irrelevant to the
            # event — the event-context dampener already cut p_literal, but
            # the market also prices it low, so BUY_NO edge is near-zero
            # while retaining full downside when the phrase appears anyway.
            if (tentative_side == "BUY_NO"
                    and "OFF_TOPIC" in reason_codes):
                reason_codes.append("OFF_TOPIC_NO_BLOCK")
                tentative_side = "WATCH"

            # ── Wide-spread BUY_NO block ──────────────────────────────────
            # Live data (118 bets): WIDE_SPREAD BUY_NO → -$2.97; but
            # WIDE_SPREAD BUY_YES → +$1.40 (market uncertainty + model
            # conviction = edge). Wide bid-ask means thin book and poor
            # entry price for NO positions. Keep YES open.
            if (tentative_side == "BUY_NO"
                    and yes_spread > 0.05):
                reason_codes.append("WIDE_SPREAD_NO_BLOCK")
                tentative_side = "WATCH"

            # ── NBA thin-book BUY_NO block ────────────────────────────────
            # Live data (24 NBA bets, tight spread + depth_no < 50):
            #   -$5.12 total, -$0.213/bet average.
            # NBA broadcast markets with thin NO-side liquidity but a tight
            # spread are systematically mispriced: the market maker quotes a
            # narrow price without real size behind it. The apparent edge
            # evaporates at execution and our base-rate signal is unreliable
            # against a shallow book.
            # MLB and NCAAB thin-book BUY_NOs remain profitable (+$2.05 and
            # +$2.10 on the same population), so this gate is NBA-only.
            # Wide-spread thin books are already caught by WIDE_SPREAD_NO_BLOCK
            # (yes_spread > 0.05), so this covers the remaining tight-spread cases.
            if (tentative_side == "BUY_NO"
                    and speaker.lower() == "nba"
                    and depth_no < 50):
                reason_codes.append("NBA_THIN_BOOK_BLOCK")
                tentative_side = "WATCH"

            # ── NBA BUY_NO price floor ────────────────────────────────────
            # Live data: NBA BUY_NO where yes_ask < 0.38 → 17 bets, -$1.61
            # (-$0.095/bet).  When the market already prices YES cheaply
            # (≤38¢), our NO-side edge is minimal and base-rate data for NBA
            # phrases is too thin (104 resolved outcomes vs 5600+ for Trump)
            # to override a market at that price level.
            if (tentative_side == "BUY_NO"
                    and speaker.lower() == "nba"
                    and yes_ask < 0.38):
                reason_codes.append("NBA_LOW_PRICE_BLOCK")
                tentative_side = "WATCH"

            # ── Removed markets: block all signals ──────────────────────────
            # These markets/speakers have been fully removed from tracking.
            # Block at gate level so any stale cards are suppressed.
            _REMOVED_SERIES = {
                "KXHOCHULMENTION", "KXNEWSOMMENTION", "KXSTARMERMENTIONB",
                "KXFOXNEWSMENTION", "KXLASTWORDMENTION", "KXLASTWORDCOUNT",
                "KXSNLMENTION", "KXMRBEASTMENTION", "KXWHPRESSBRIEFING",
                # Removed 2026-03-30 (second pass):
                "KXCARNEYMENTION", "KXHOMANMENTION", "KXMENTION",
                "KXPERSONMENTION", "KXPOLITICSMENTION", "KXNYCMDEBMENTION",
                "KXMENTIONEARNDAL", "KXSURVIVORMENTION", "KXWBCMENTION",
                "KXENTMENTION", "KXEARNINGSMENTIONULTA", "KXEARNINGSMENTIONEA",
                "KXEARNINGSMENTIONADBE", "KXJENSENMENTION",
                # Additional removed (2026-04-05):
                "KXMTPMENTION", "KXFTNMENTION", "KXMADDOWMENTION",
                "KXPELOSIMENTION", "KXINFANTINOMENTION", "KXDIMONMENTION",
                "KXBARRMENTION", "KXSCOTUSMENTION",
            }
            _market_series = market_id.split("-")[0].upper() if market_id else ""
            _removed_speaker = speaker.lower() in {
                "hochul", "newsom", "starmer", "snl", "mrbeast", "whitehouse",
                "carney", "homan", "auto", "survivor", "wbc",
                "entertainment", "ulta", "ea", "adobe", "jensen",
            }
            if _market_series in _REMOVED_SERIES or _removed_speaker:
                reason_codes.append("REMOVED_MARKET")
                tentative_side = "WATCH"

            # ── Trump BUY_NO block ───────────────────────────────────────────
            # Live data (29 bets): Trump BUY_NO → 37.9% WR, -$4.87.  The model
            # consistently underestimates Trump's phrase usage during live events.
            # Block all Trump BUY_NO — only YES side is reliably profitable.
            if (tentative_side == "BUY_NO"
                    and _is_trump):
                reason_codes.append("TRUMP_NO_BLOCK")
                tentative_side = "WATCH"

            # ── Trump BUY_YES quality gate ──────────────────────────────────
            # Live data (27 bets): unfiltered Trump BUY_YES → 44% WR, +$1.54.
            # Sweet spot is p >= 0.40 AND ask 0.35-0.50 → 70% WR, +$2.80.
            # Lowered p floor from 0.40 → 0.35 to recover underpriced phrases
            # (shutdown p=0.38/ask=0.17, stock market p=0.38/ask=0.19) that have
            # strong EV (+0.28-0.32) but were blocked by the tighter threshold.
            # Expensive asks (>0.50) remain blocked — signal already priced in.
            if (tentative_side == "BUY_YES"
                    and _is_trump
                    and (p_literal < 0.35 or yes_ask > 0.50)):
                reason_codes.append("TRUMP_YES_QUALITY_GATE")
                tentative_side = "WATCH"

            # ── Leavitt BUY_YES quality gate ─────────────────────────────────
            # Historical data (10 bets): 20% WR, -$1.73 — driven by low-confidence
            # signals at mixed ask prices.  Replacing full block with a threshold
            # gate: only allow high-model-confidence phrases at genuinely underpriced
            # asks.  p_literal >= 0.60 means model is highly confident Leavitt says
            # this phrase; yes_ask <= 0.45 ensures the market hasn't priced it in.
            # "radical left" (p=0.78, ask=0.19), "china" (p=0.68, ask=0.19) pass.
            if (tentative_side == "BUY_YES"
                    and (speaker or "").lower().strip() == "leavitt"
                    and (p_literal < 0.60 or yes_ask > 0.45)):
                reason_codes.append("LEAVITT_YES_BLOCK")
                tentative_side = "WATCH"

            # ── Thin-book BUY_NO block (all speakers) ────────────────────
            # NBA_THIN_BOOK_BLOCK covers NBA + depth_no < 50.
            # Extend to ALL speakers when SCORE_CONF_HIGH + thin book.
            # Session 65 P4.1: CONF_HIGH+THIN BUY_NO → 39.1% WR, -$1.06
            # on 23 non-NBA bets. High confidence thin books often reflect
            # automated market makers quoting tight/thin without real size.
            if (tentative_side == "BUY_NO"
                    and speaker.lower() not in ("nba",)  # NBA has its own gate
                    and depth_no < 50
                    and yes_spread <= 0.05             # tight spread = automated mm
                    and "SCORE_CONF_HIGH" in reason_codes):
                reason_codes.append("THIN_BOOK_CONF_BLOCK")
                tentative_side = "WATCH"

            # ── Thin speaker penalty ─────────────────────────────────────
            # Speakers with very few resolved historical outcomes in our
            # calibration data get unreliable base rates.  Block bets unless
            # the model has at least moderate confidence from other signals.
            if tentative_side in ("BUY_YES", "BUY_NO"):
                _known_speakers = {
                    "trump":    5652,
                    "mamdani":   924,
                    "leavitt":   204,
                    # "starmer": removed from tracking
                    "hegseth":    80,
                    # New speakers — corpus being built, base_rates.yaml priors set
                    "powell":    150,   # Fed chair press conferences (formulaic)
                    # "carney": removed from tracking
                    # "homan": removed from tracking
                    # Structured-format speakers — no base-rate corpus but
                    # certainty extractor provides case/conference-specific
                    # p_floors so THIN_SPEAKER gate should allow them through
                    # when a p_floor or p_override is present (score_confidence
                    # will be elevated by the event_signals multiplier).
                    "fed":      150,    # Powell press conferences — formulaic
                    "witness":  100,    # Congressional hearing witnesses
                    "scotus":   100,    # SCOTUS oral argument justices
                    # Earnings/entertainment speakers — removed from tracking:
                    # "earnings", "jensen", "auto", "ea", "ulta", "adobe", "entertainment"
                    # Sports broadcast announcers — phrase priors set in
                    # config/base_rates_priors.yaml; no corpus transcripts but
                    # phrase patterns are structurally predictable (venue names,
                    # play types, player references).
                    "nba":      500,    # NBA broadcast announcer markets
                    "ncaab":    300,    # NCAAB broadcast announcer markets
                    "mlb":      300,    # MLB broadcast announcer markets
                    "mma":      200,    # MMA/fight broadcast announcer markets
                }
                sp = (speaker or "").lower().strip()
                sp_outcomes = _known_speakers.get(sp, 0)
                if sp_outcomes < 100 and score_confidence < 0.65:
                    reason_codes.append("THIN_SPEAKER")
                    tentative_side = "WATCH"

            # ── ¼ Kelly gate ────────────────────────────────────────────────
            if tentative_side == "BUY_YES":
                kf = kelly_fraction(ev_yes, 1.0 - yes_ask)
                components["kelly_fraction"] = round(kf, 4)
                if kf < KELLY_MIN_FRACTION:
                    reason_codes.append("KELLY_WEAK")
                    tentative_side = "WATCH"
            elif tentative_side == "BUY_NO":
                kf = kelly_fraction(ev_no, 1.0 - no_ask)
                components["kelly_fraction"] = round(kf, 4)
                if kf < KELLY_MIN_FRACTION:
                    reason_codes.append("KELLY_WEAK")
                    tentative_side = "WATCH"

            # Spread gate: for prediction markets held to resolution, the bid price
            # is irrelevant — only the ask matters (entry price).  The spread check
            # is only meaningful as a proxy for market efficiency / liquidity.
            # For BUY_NO we skip the spread check entirely and rely solely on
            # depth and EV; the YES bid-ask being wide has no bearing on whether
            # we can profitably enter a NO position.
            # For BUY_YES we keep a loose spread check (yes_spread <= max_spread)
            # so we avoid penny-wide books where the ask is artificially quoted.
            if tentative_side == "BUY_YES":
                spread_ok = yes_spread <= self.max_spread
            else:
                # BUY_NO or WATCH — don't gate on spread
                spread_ok = True

            depth_for_side = depth_yes if tentative_side == "BUY_YES" else depth_no
            depth_ok = (tentative_side == "WATCH") or (depth_for_side >= self.min_depth)
            gate_pass = spread_ok and depth_ok

            if gate_pass:
                reason_codes.append("GATE_PASS")
            else:
                reason_codes.append("GATE_FAIL")
                if not spread_ok:
                    reason_codes.append("SPREAD_TOO_WIDE")
                if not depth_ok:
                    reason_codes.append("DEPTH_TOO_THIN")

            if tentative_side == "BUY_YES" and depth_yes < 50:
                reason_codes.append("LOW_DEPTH")
            elif tentative_side == "BUY_NO" and depth_no < 50:
                reason_codes.append("LOW_DEPTH")
            # Informational tag: YES bid-ask is wide (not a gate, just FYI)
            if yes_spread > 0.05:
                reason_codes.append("WIDE_SPREAD")

            side = tentative_side if gate_pass else "WATCH"
            side = self._apply_guardrails(
                side,
                event=event,
                hit_today=hit_today,
                reason_codes=reason_codes,
                ev_yes=ev_yes,
                yes_ask=yes_ask,
                no_ask=no_ask,
            )

            size_rec = self._size_rec(reason_codes) if side in ("BUY_YES", "BUY_NO") else 0.0
            exec_hint = self._exec_hint(side, yes_ask, no_ask, size_rec)
            size_cap = self._size_cap(side, depth_yes, depth_no)

            event_context: dict = {}
            time_remaining: float | None = None
            if event:
                event_context = {
                    "event_id": event.event_id,
                    "speech_state": event.speech_state,
                    "event_type": event.event_type,
                }
                if event.speech_state == "live":
                    elapsed = self._elapsed_sec(event, now_dt)
                    dur = event.expected_duration_sec or 5400
                    time_remaining = max(0.0, dur - elapsed)
                elif event.speech_state == "scheduled" and event.scheduled_start_ts:
                    start = datetime.fromisoformat(event.scheduled_start_ts)
                    if start.tzinfo is None:
                        start = start.replace(tzinfo=timezone.utc)
                    secs_until = (start - now_dt).total_seconds()
                    if secs_until > 0:
                        event_context["starts_in_sec"] = round(secs_until)
            event_context["market_family"] = market_family_name
            event_context["series_ticker"] = series_ticker
            if market_family_name == "windowed":
                n_rem = self._remaining_events_n(market_id, now_dt)
                if n_rem is not None:
                    event_context["events_remaining"] = round(n_rem, 2)
                else:
                    frac_remaining = self._remaining_window_fraction(market_id, now_dt)
                    if frac_remaining is not None:
                        event_context["window_fraction_remaining"] = round(frac_remaining, 4)

            _calib_str = f"→{components['p_calibrated']:.3f}" if components.get("p_calibrated") else ""
            rationale = (
                f"side={side} p={p_literal:.3f}{_calib_str} reasons={','.join(reason_codes)}; "
                f"ev_yes={ev_yes:+.4f} ev_no={ev_no:+.4f}; "
                f"spread={spread:.4f}(<={self.max_spread:.4f}:{spread_ok}) "
                f"depth={depth_yes:.0f}(>={self.min_depth:.0f}:{depth_ok})"
            )

            _p_calibrated   = components.get("p_calibrated", round(p_literal, 4))
            _calib_shift    = components.get("calib_shift")
            _kl_divergence  = components.get("kl_divergence")
            _kelly_fraction = components.get("kelly_fraction")

            _score_payload: dict = {
                "p_literal": round(p_literal, 4),
                "p_calibrated": _p_calibrated,
                "calib_shift": _calib_shift,
                "base_rate": components["base_rate"],
                "time_decay": components["time_decay"],
                "news_pressure": components["news_pressure"],
                "x_buzz": components["x_buzz"],
                "kl_divergence": _kl_divergence,
                "kelly_fraction": _kelly_fraction,
                "ev_yes": ev_yes,
                "ev_no": ev_no,
                "risk_adjusted_ev_yes": risk_adjusted_ev_yes,
                "risk_adjusted_ev_no": risk_adjusted_ev_no,
                "score_confidence": score_confidence,
                "effective_ev_threshold": effective_ev_threshold,
            }
            if components.get("same_story_provenance_trim"):
                _score_payload["same_story_provenance_trim"] = components["same_story_provenance_trim"]
            if components.get("source_story_hashes"):
                _score_payload["source_story_hashes"] = components["source_story_hashes"]
            if components.get("news_story_hashes"):
                _score_payload["news_story_hashes"] = components["news_story_hashes"]

            payload = {
                "ts": now,
                "market_id": market_id,
                "subject": speaker,
                "phrase": phrase,
                "side": side,
                "p_literal": round(p_literal, 4),
                "p_calibrated": _p_calibrated,
                "calib_shift": _calib_shift,
                "kl_divergence": _kl_divergence,
                "kelly_fraction": _kelly_fraction,
                "scores": _score_payload,
                "liquidity": {
                    "yes_ask": round(yes_ask, 4),
                    "no_ask": round(no_ask, 4),
                    "spread": round(spread, 4),
                    "yes_spread": round(yes_spread, 4),
                    "no_spread": round(no_spread, 4),
                    "depth_yes": depth_yes,
                    "depth_no": depth_no,
                    "spread_ok": spread_ok,
                    "depth_ok": depth_ok,
                    "gate_pass": gate_pass,
                },
                "yes_ask": round(yes_ask, 4),
                "no_ask": round(no_ask, 4),
                "ev_yes": ev_yes,
                "ev_no": ev_no,
                "risk_adjusted_ev_yes": risk_adjusted_ev_yes,
                "risk_adjusted_ev_no": risk_adjusted_ev_no,
                "risk_adjusted_ev_chosen": (
                    risk_adjusted_ev_yes if side == "BUY_YES"
                    else risk_adjusted_ev_no if side == "BUY_NO"
                    else max(risk_adjusted_ev_yes, risk_adjusted_ev_no)
                ),
                "score_confidence": score_confidence,
                "effective_ev_threshold": effective_ev_threshold,
                "exec_price_hint": exec_hint,
                "size_rec": size_rec,
                "size_cap": size_cap,
                "spread_ok": spread_ok,
                "depth_ok": depth_ok,
                "gate_pass": gate_pass,
                "reason_codes": reason_codes,
                "event": event_context,
                "time_remaining_sec": time_remaining,
                "rationale": rationale,
                "poly_yes": round(poly_yes, 4) if poly_yes is not None else None,
                "poly_confidence": poly_confidence,
                "poly_confidence_bucket": poly_confidence_bucket,
                "wallet_alpha_score": wallet_alpha_score,
                "wallet_confidence": wallet_confidence,
                "wallet_bias": wallet_bias,
                "wallet_extreme_bets": int(components.get("wallet_extreme_bets", 0) or 0),
                "wallet_reputation_alpha": float(components.get("wallet_reputation_alpha", 0.0) or 0.0),
                "cross_market_edge": cross_market_edge,
                "poly_strong_match": poly_has_strong_match,
            }
            is_material = self._is_material_change(market_id, side, ev_yes, ev_no,
                                                    event.speech_state if event else None,
                                                    now_dt,
                                                    effective_ev_threshold=effective_ev_threshold)

            # Collect for two-pass EV-sorted EVENT_BET_CAP
            _evt_key = event_ticker or market_family_name or "unknown"
            _ev_chosen = (
                ev_yes if side == "BUY_YES"
                else ev_no if side == "BUY_NO"
                else 0.0
            )
            _pending_payloads.append((_evt_key, _ev_chosen, payload, is_material))

        # ── Pass 2: apply EVENT_BET_CAP + portfolio risk cap ──────────────────
        # Sort BUY cards by EV within each event so the cap keeps the best-EV
        # bets instead of the alphabetically-first markets (old behaviour).
        _event_buy_counts: dict[str, int] = defaultdict(int)
        _event_risk_dollars: dict[str, float] = defaultdict(float)
        _pending_payloads.sort(
            key=lambda x: (
                x[0],  # group by event key
                0 if x[2]["side"] in ("BUY_YES", "BUY_NO") else 1,
                -x[1],  # descending EV within BUY cards
            )
        )

        for _evt_key, _ev_chosen, payload, is_material in _pending_payloads:
            _side = payload["side"]
            if _side in ("BUY_YES", "BUY_NO"):
                # Count-based cap: max 10 BUY bets per event
                _count_exceeded = _event_buy_counts[_evt_key] >= MAX_BUY_PER_EVENT
                # Dollar-risk cap: max $150 at risk per event (portfolio correlation guard).
                # Use size_rec (confidence-tiered $5/$10/$20) for risk accounting so
                # high-confidence bets use their full $20 allocation while lower-confidence
                # bets stay proportionally smaller.
                _size = float(payload.get("size_rec") or self._MAX_BET_SIZE)
                _price = float(payload.get("yes_ask" if _side == "BUY_YES" else "no_ask",
                               payload.get("yes_ask", 0.5)))
                _bet_risk = _size * _price  # $ at risk for this single bet
                _risk_exceeded = (_event_risk_dollars[_evt_key] + _bet_risk) > MAX_RISK_PER_EVENT
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

            # Only write a new DB row when the recommendation is actionable or the
            # side changed.  Successive WATCH cards for the same market produce no
            # new information — the dashboard selects MAX(id) per market anyway.
            # This is the primary control on action_cards table size (~100× reduction).
            _mkt_id = payload["market_id"]
            _new_side = payload["side"]
            _should_write = is_material or (self._last_recorded_side.get(_mkt_id) != _new_side)
            if _should_write:
                self.conn.execute(
                    """
                    INSERT INTO action_cards (
                        ts, market_id, phrase, side, p_literal,
                        yes_ask, no_ask, ev_yes, ev_no,
                        exec_price_hint, size_cap,
                        spread_ok, depth_ok, gate_pass, rationale, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
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
            logger.debug("Scored %d markets, %d material notifications", count, notified)
        return count


def _compute_cross_market_edge(
    *,
    poly_yes: float,
    kalshi_yes: float,
) -> float | None:
    """Detect cross-platform arbitrage: if you buy YES on the cheaper and NO on the
    expensive platform, what's the guaranteed profit per share?

    Returns profit per $1 share, or None if no arb exists.
    """
    if poly_yes <= 0 or kalshi_yes <= 0:
        return None
    if poly_yes < kalshi_yes:
        cost = poly_yes + (1.0 - kalshi_yes)
    else:
        cost = kalshi_yes + (1.0 - poly_yes)
    profit = 1.0 - cost
    return profit if profit > 0 else None


def _clamp_p(p: float) -> float:
    return max(P_MIN, min(P_MAX, p))
