"""Tests for BayesianScorer — hierarchical Beta-Binomial mispricing detector."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from app.bayesian_rates import BayesianRate, BayesianRateStore
from app.bayesian_scorer import BayesianScorer
from app.event_detector import EventDetector
from app.utils import utc_now_iso


def _today() -> str:
    return utc_now_iso()[:10]


def _insert_market(conn, market_id="KXTRUMPMENTION-26MAR07-NATO", subject="trump"):
    conn.execute(
        "INSERT OR IGNORE INTO markets (market_id, slug, subject, prompt) "
        "VALUES (?, ?, ?, ?)",
        (market_id, f"slug-{market_id}", subject, f"prompt for {market_id}"),
    )
    conn.commit()


def _insert_snapshot(
    conn,
    market_id="KXTRUMPMENTION-26MAR07-NATO",
    yes_bid=0.30,
    yes_ask=0.35,
    no_bid=0.60,
    no_ask=0.65,
    spread=0.05,
    depth_yes=200,
    depth_no=200,
    volume_1h=500,
):
    ts = utc_now_iso()
    conn.execute(
        """INSERT INTO market_snapshots
        (ts, market_id, yes_bid, yes_ask, no_bid, no_ask, spread,
         depth_yes, depth_no, volume_1h, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')""",
        (ts, market_id, yes_bid, yes_ask, no_bid, no_ask, spread,
         depth_yes, depth_no, volume_1h),
    )
    conn.commit()


def _insert_live_event(conn, speaker="trump"):
    from datetime import datetime, timezone
    event_id = f"{speaker}:test:rally-01"
    start = datetime.now(tz=timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO events "
        "(event_id, speaker, event_type, speech_state, event_start_ts, expected_duration_sec) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, speaker, "rally", "live", start, 5400),
    )
    conn.commit()


def _make_bayesian_rates_json(
    tmp_dir: Path,
    rates: list[dict] | None = None,
    speaker_priors: dict | None = None,
) -> Path:
    """Write a bayesian_rates.json file and return a BayesianRateStore."""
    if rates is None:
        rates = [
            {
                "key": "trump.nato",
                "alpha": 8.0,
                "beta": 12.0,
                "n_obs": 20,
                "mean": 0.40,
                "ci_low": 0.25,
                "ci_high": 0.55,
                "confidence": 0.70,
            },
        ]
    data = {
        "computed_at": utc_now_iso(),
        "prior_strength": 10.0,
        "halflife_days": 60.0,
        "total_phrases": len(rates),
        "global_mean": 0.45,
        "speaker_priors": speaker_priors or {"trump": 0.46},
        "rates": rates,
    }
    path = tmp_dir / "bayesian_rates.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _make_scorer(conn, tmp_dir, rates=None, speaker_priors=None, **kwargs):
    path = _make_bayesian_rates_json(tmp_dir, rates=rates, speaker_priors=speaker_priors)
    store = BayesianRateStore.from_json(path)
    detector = EventDetector(conn=conn)
    market_phrases = kwargs.pop("market_phrases", {
        "KXTRUMPMENTION-26MAR07-NATO": ["nato", "n.a.t.o."],
    })
    return BayesianScorer(
        conn=conn,
        action_cards_path=tmp_dir / "cards.jsonl",
        event_detector=detector,
        bayesian_rates=store,
        market_phrases=market_phrases,
        **kwargs,
    )


# ── Basic functionality tests ────────────────────────────────────────────

def test_run_once_returns_count(tmp_db, tmp_dir):
    """run_once returns 0 when no markets exist."""
    scorer = _make_scorer(tmp_db, tmp_dir)
    assert scorer.run_once() == 0


def test_run_once_with_market_no_data(tmp_db, tmp_dir):
    """Market with no Bayesian data → WATCH (NO_PHRASE_DATA)."""
    _insert_market(tmp_db)
    _insert_snapshot(tmp_db)
    scorer = _make_scorer(tmp_db, tmp_dir, rates=[])
    count = scorer.run_once()
    assert count == 1  # market is scored but falls to WATCH
    cards = json.loads((tmp_dir / "cards.jsonl").read_text().strip())
    assert cards["side"] == "WATCH"
    assert "NO_PHRASE_DATA" in cards["reason_codes"]


def test_buy_yes_when_market_underprices(tmp_db, tmp_dir):
    """When yes_ask < ci_low → BUY_YES (market underprices YES)."""
    rates = [{
        "key": "trump.nato",
        "alpha": 30.0,
        "beta": 20.0,
        "n_obs": 50,
        "mean": 0.60,
        "ci_low": 0.48,
        "ci_high": 0.72,
        "confidence": 0.76,
    }]
    _insert_market(tmp_db)
    _insert_snapshot(tmp_db, yes_ask=0.30, no_ask=0.70)
    scorer = _make_scorer(tmp_db, tmp_dir, rates=rates)
    count = scorer.run_once()
    assert count == 1

    cards = json.loads((tmp_dir / "cards.jsonl").read_text().strip())
    assert cards["side"] == "BUY_YES"
    assert "CI_EXCLUDES_MARKET_YES" in cards["reason_codes"]


def test_buy_no_when_market_overprices(tmp_db, tmp_dir):
    """When yes_ask > ci_high → BUY_NO (market overprices YES)."""
    rates = [{
        "key": "trump.nato",
        "alpha": 5.0,
        "beta": 45.0,
        "n_obs": 50,
        "mean": 0.10,
        "ci_low": 0.04,
        "ci_high": 0.18,
        "confidence": 0.86,
    }]
    _insert_market(tmp_db)
    _insert_snapshot(tmp_db, yes_ask=0.35, no_ask=0.45, no_bid=0.40)
    scorer = _make_scorer(tmp_db, tmp_dir, rates=rates)
    count = scorer.run_once()
    assert count == 1

    cards = json.loads((tmp_dir / "cards.jsonl").read_text().strip())
    assert cards["side"] == "BUY_NO"
    assert "CI_EXCLUDES_MARKET_NO" in cards["reason_codes"]


def test_watch_when_ci_includes_market(tmp_db, tmp_dir):
    """When market price falls within CI → WATCH."""
    rates = [{
        "key": "trump.nato",
        "alpha": 10.0,
        "beta": 10.0,
        "n_obs": 20,
        "mean": 0.50,
        "ci_low": 0.35,
        "ci_high": 0.65,
        "confidence": 0.70,
    }]
    _insert_market(tmp_db)
    _insert_snapshot(tmp_db, yes_ask=0.48, no_ask=0.52)
    scorer = _make_scorer(tmp_db, tmp_dir, rates=rates)
    count = scorer.run_once()
    assert count == 1

    cards = json.loads((tmp_dir / "cards.jsonl").read_text().strip())
    assert cards["side"] == "WATCH"


def test_settled_market_blocks_no(tmp_db, tmp_dir):
    """BUY_NO blocked when yes_ask >= 0.85 (market effectively settled YES)."""
    rates = [{
        "key": "trump.nato",
        "alpha": 2.0,
        "beta": 48.0,
        "n_obs": 50,
        "mean": 0.04,
        "ci_low": 0.01,
        "ci_high": 0.09,
        "confidence": 0.92,
    }]
    _insert_market(tmp_db)
    _insert_snapshot(tmp_db, yes_ask=0.90, no_ask=0.10, no_bid=0.08)
    scorer = _make_scorer(tmp_db, tmp_dir, rates=rates)
    count = scorer.run_once()
    assert count == 1

    cards = json.loads((tmp_dir / "cards.jsonl").read_text().strip())
    assert cards["side"] == "WATCH"
    assert "SETTLED_MARKET_BLOCK" in cards["reason_codes"]


def test_cheap_no_blocks(tmp_db, tmp_dir):
    """BUY_NO blocked when no_ask < 0.40."""
    rates = [{
        "key": "trump.nato",
        "alpha": 5.0,
        "beta": 45.0,
        "n_obs": 50,
        "mean": 0.10,
        "ci_low": 0.04,
        "ci_high": 0.18,
        "confidence": 0.86,
    }]
    _insert_market(tmp_db)
    _insert_snapshot(tmp_db, yes_ask=0.70, no_ask=0.30, no_bid=0.25)
    scorer = _make_scorer(tmp_db, tmp_dir, rates=rates)
    count = scorer.run_once()
    assert count == 1

    cards = json.loads((tmp_dir / "cards.jsonl").read_text().strip())
    assert cards["side"] == "WATCH"
    assert "CHEAP_NO_BLOCK" in cards["reason_codes"]


def test_buy_yes_passes_kelly_with_real_edge(tmp_db, tmp_dir):
    """BUY_YES with meaningful edge passes Kelly gate."""
    rates = [{
        "key": "trump.nato",
        "alpha": 25.0,
        "beta": 25.0,
        "n_obs": 50,
        "mean": 0.50,
        "ci_low": 0.38,
        "ci_high": 0.62,
        "confidence": 0.76,
    }]
    _insert_market(tmp_db)
    # yes_ask=0.37 < ci_low=0.38 → BUY_YES; EV=0.13, kelly=0.13/0.63=0.21 → passes
    _insert_snapshot(tmp_db, yes_ask=0.37, no_ask=0.63)
    scorer = _make_scorer(tmp_db, tmp_dir, rates=rates)
    count = scorer.run_once()
    assert count == 1

    cards = json.loads((tmp_dir / "cards.jsonl").read_text().strip())
    assert cards["side"] == "BUY_YES"
    assert "CI_EXCLUDES_MARKET_YES" in cards["reason_codes"]


def test_kelly_weak_blocks_thin_edge(tmp_db, tmp_dir):
    """BUY_YES blocked when Kelly fraction < 5% (thin edge)."""
    # alpha=2000, beta=1960 → mean=0.5051, ci_low=0.492 > ask=0.48 → BUY_YES
    # but ev=0.025, kelly=0.025/0.52=0.048 < 0.05 → KELLY_WEAK
    rates = [{
        "key": "trump.nato",
        "alpha": 2000.0,
        "beta": 1960.0,
        "n_obs": 3960,
        "mean": 0.5051,
        "ci_low": 0.492,
        "ci_high": 0.518,
        "confidence": 0.97,
    }]
    _insert_market(tmp_db)
    _insert_snapshot(tmp_db, yes_ask=0.48, no_ask=0.52)
    scorer = _make_scorer(tmp_db, tmp_dir, rates=rates)
    count = scorer.run_once()
    assert count == 1

    cards = json.loads((tmp_dir / "cards.jsonl").read_text().strip())
    assert cards["side"] == "WATCH"
    assert "KELLY_WEAK" in cards["reason_codes"]


def test_low_confidence_watch(tmp_db, tmp_dir):
    """Phrase with low confidence (wide CI) → WATCH."""
    rates = [{
        "key": "trump.nato",
        "alpha": 3.0,
        "beta": 3.0,
        "n_obs": 4,
        "mean": 0.50,
        "ci_low": 0.15,
        "ci_high": 0.85,
        "confidence": 0.30,
    }]
    _insert_market(tmp_db)
    _insert_snapshot(tmp_db, yes_ask=0.10, no_ask=0.90)
    scorer = _make_scorer(tmp_db, tmp_dir, rates=rates)
    count = scorer.run_once()
    assert count == 1

    cards = json.loads((tmp_dir / "cards.jsonl").read_text().strip())
    assert cards["side"] == "WATCH"
    assert "LOW_CONFIDENCE" in cards["reason_codes"]


def test_phrase_hit_forces_buy_yes(tmp_db, tmp_dir):
    """When phrase was confirmed in transcript → BUY_YES override."""
    rates = [{
        "key": "trump.nato",
        "alpha": 5.0,
        "beta": 45.0,
        "n_obs": 50,
        "mean": 0.10,
        "ci_low": 0.04,
        "ci_high": 0.18,
        "confidence": 0.86,
    }]
    _insert_market(tmp_db)
    _insert_snapshot(tmp_db, yes_ask=0.50, no_ask=0.50)

    # Insert a phrase hit for today
    ts = utc_now_iso()
    tmp_db.execute(
        "INSERT INTO transcripts (ts, source, source_ref, text, text_hash) "
        "VALUES (?, 'test', 'ref', 'talked about nato', 'hash123')",
        (ts,),
    )
    tid = tmp_db.execute("SELECT last_insert_rowid()").fetchone()[0]
    tmp_db.execute(
        "INSERT INTO phrase_hits (ts, transcript_id, phrase, start_idx, end_idx, snippet, hit_date) "
        "VALUES (?, ?, ?, 0, 4, 'nato expansion', ?)",
        (ts, tid, "nato", _today()),
    )
    tmp_db.commit()

    scorer = _make_scorer(tmp_db, tmp_dir, rates=rates)
    count = scorer.run_once()
    assert count == 1

    cards = json.loads((tmp_dir / "cards.jsonl").read_text().strip())
    assert cards["side"] == "BUY_YES"
    assert "PHRASE_HIT" in cards["reason_codes"]


def test_event_bet_cap(tmp_db, tmp_dir):
    """Only MAX_BUY_PER_EVENT bets per event, best EV first."""
    rates_list = []
    market_phrases = {}
    for i in range(15):
        phrase = f"phrase{i}"
        market_id = f"KXTRUMPMENTION-26MAR07-{phrase.upper()}"
        rates_list.append({
            "key": f"trump.{phrase}",
            "alpha": 40.0,
            "beta": 10.0,
            "n_obs": 50,
            "mean": 0.80,
            "ci_low": 0.68,
            "ci_high": 0.90,
            "confidence": 0.78,
        })
        market_phrases[market_id] = [phrase]
        _insert_market(tmp_db, market_id=market_id, subject="trump")
        _insert_snapshot(tmp_db, market_id=market_id,
                         yes_ask=0.30, no_ask=0.70)

    scorer = _make_scorer(
        tmp_db, tmp_dir,
        rates=rates_list,
        market_phrases=market_phrases,
    )
    count = scorer.run_once()
    assert count == 15

    lines = (tmp_dir / "cards.jsonl").read_text().strip().split("\n")
    buy_count = sum(
        1 for line in lines
        if json.loads(line)["side"] in ("BUY_YES", "BUY_NO")
    )
    assert buy_count <= 10  # MAX_BUY_PER_EVENT


def test_payload_format_compatibility(tmp_db, tmp_dir):
    """Verify BayesianScorer emits payloads with all required fields."""
    rates = [{
        "key": "trump.nato",
        "alpha": 35.0,
        "beta": 15.0,
        "n_obs": 50,
        "mean": 0.70,
        "ci_low": 0.58,
        "ci_high": 0.82,
        "confidence": 0.76,
    }]
    _insert_market(tmp_db)
    _insert_snapshot(tmp_db, yes_ask=0.30, no_ask=0.70)
    scorer = _make_scorer(tmp_db, tmp_dir, rates=rates)
    scorer.run_once()

    card = json.loads((tmp_dir / "cards.jsonl").read_text().strip())

    required_fields = [
        "ts", "market_id", "subject", "phrase", "side",
        "p_literal", "yes_ask", "no_ask", "ev_yes", "ev_no",
        "exec_price_hint", "size_rec", "size_cap",
        "spread_ok", "depth_ok", "gate_pass",
        "reason_codes", "event", "rationale",
    ]
    for field in required_fields:
        assert field in card, f"Missing field: {field}"


def test_db_write(tmp_db, tmp_dir):
    """Verify BayesianScorer writes to action_cards table."""
    rates = [{
        "key": "trump.nato",
        "alpha": 35.0,
        "beta": 15.0,
        "n_obs": 50,
        "mean": 0.70,
        "ci_low": 0.58,
        "ci_high": 0.82,
        "confidence": 0.76,
    }]
    _insert_market(tmp_db)
    _insert_snapshot(tmp_db, yes_ask=0.30, no_ask=0.70)
    scorer = _make_scorer(tmp_db, tmp_dir, rates=rates)
    scorer.run_once()

    row = tmp_db.execute("SELECT * FROM action_cards ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["side"] == "BUY_YES"
    assert row["p_literal"] == pytest.approx(0.70, abs=0.01)


# ── BayesianRate dataclass tests ──────────────────────────────────────────

def test_bayesian_rate_properties():
    """Test BayesianRate CI and confidence computations."""
    rate = BayesianRate(alpha=20.0, beta=30.0, n_obs=40)
    assert rate.mean == pytest.approx(0.40, abs=0.01)
    assert 0.0 < rate.ci_low < rate.mean
    assert rate.mean < rate.ci_high < 1.0
    assert 0.0 <= rate.confidence <= 1.0


def test_bayesian_rate_extreme_alpha():
    """When alpha >> beta, mean → 1.0 and CI is tight."""
    rate = BayesianRate(alpha=100.0, beta=5.0, n_obs=95)
    assert rate.mean > 0.90
    assert rate.confidence > 0.80


def test_bayesian_rate_extreme_beta():
    """When beta >> alpha, mean → 0.0 and CI is tight."""
    rate = BayesianRate(alpha=5.0, beta=100.0, n_obs=95)
    assert rate.mean < 0.10
    assert rate.confidence > 0.80
