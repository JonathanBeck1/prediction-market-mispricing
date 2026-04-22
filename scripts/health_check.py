#!/usr/bin/env python3
"""Event-day health checks for freshness, coverage, and signal completeness."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.dashboard import (
    EVENTS_CACHE,
    KALSHI_CACHE,
    _load_latest_snapshot_market_meta,
    _load_market_cache,
    _load_outcome_rates,
    _query_confirmed_phrases,
    _query_all_cards,
    _query_coverage,
)
from app.db import init_db


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _age_sec(ts: str, now: datetime) -> float:
    dt = _parse_ts(ts)
    if dt is None:
        return 10**9
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (now - dt.astimezone(timezone.utc)).total_seconds())


def _status(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def _file_age_sec(path: Path, now: datetime) -> float:
    try:
        mtime = path.stat().st_mtime
    except Exception:
        return 10**9
    return max(0.0, now.timestamp() - float(mtime))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-snapshot-age-sec", type=float, default=300.0)
    parser.add_argument("--max-card-age-sec", type=float, default=300.0)
    parser.add_argument("--max-untracked-events", type=int, default=50)
    parser.add_argument("--max-market-cache-age-sec", type=float, default=900.0)
    parser.add_argument("--max-events-cache-age-sec", type=float, default=900.0)
    parser.add_argument("--max-discovered-no-snapshots", type=int, default=3)
    parser.add_argument("--max-discovered-no-snapshots-age-sec", type=float, default=900.0)
    parser.add_argument("--min-poly-linked-ratio", type=float, default=0.05)
    parser.add_argument("--min-wallet-present-ratio", type=float, default=0.01)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    conn = init_db(db_path=Path("data/edge.db"))
    now = datetime.now(timezone.utc)

    latest_snap = conn.execute("SELECT MAX(ts) FROM market_snapshots").fetchone()[0] or ""
    latest_card = conn.execute("SELECT MAX(ts) FROM action_cards").fetchone()[0] or ""
    snapshot_age = _age_sec(latest_snap, now)
    card_age = _age_sec(latest_card, now)
    market_cache_age = _file_age_sec(KALSHI_CACHE, now)
    events_cache_age = _file_age_sec(EVENTS_CACHE, now)

    market_cache = _load_market_cache()
    outcome_rates = _load_outcome_rates()
    confirmed_phrases = _query_confirmed_phrases(conn)
    all_cards = _query_all_cards(conn, market_cache, outcome_rates, confirmed_phrases)
    _ = _load_latest_snapshot_market_meta(conn)
    coverage = _query_coverage(conn, market_cache, all_cards)
    coverage_events = coverage.get("events", [])
    discovered_no_snapshots = [
        e for e in coverage_events if "EVENT_DISCOVERED_NO_SNAPSHOTS" in set(e.get("reason_codes", []))
    ]
    max_discovered_no_snapshots_age = 0.0
    for e in discovered_no_snapshots:
        ts = str((e or {}).get("first_seen_at") or "")
        if ts:
            max_discovered_no_snapshots_age = max(max_discovered_no_snapshots_age, _age_sec(ts, now))

    scored_cards = [c for c in all_cards if c.get("side") in {"BUY_YES", "BUY_NO", "WATCH"}]
    poly_linked = [c for c in scored_cards if c.get("poly_yes") is not None]
    wallet_present = [c for c in scored_cards if c.get("wallet_confidence") is not None]

    poly_ratio = (len(poly_linked) / len(scored_cards)) if scored_cards else 0.0
    wallet_ratio = (len(wallet_present) / len(scored_cards)) if scored_cards else 0.0

    checks = {
        "snapshot_freshness": snapshot_age <= args.max_snapshot_age_sec,
        "action_card_freshness": card_age <= args.max_card_age_sec,
        "market_cache_freshness": market_cache_age <= args.max_market_cache_age_sec,
        "events_cache_freshness": events_cache_age <= args.max_events_cache_age_sec,
        "coverage_untracked_events": int(coverage.get("untracked_event_count", 0)) <= args.max_untracked_events,
        "coverage_discovered_no_snapshots_count": len(discovered_no_snapshots) <= args.max_discovered_no_snapshots,
        "coverage_discovered_no_snapshots_age": (
            max_discovered_no_snapshots_age <= args.max_discovered_no_snapshots_age_sec
        ),
        "poly_signal_completeness": poly_ratio >= args.min_poly_linked_ratio,
        "wallet_signal_presence": wallet_ratio >= args.min_wallet_present_ratio,
    }
    ok_all = all(checks.values())

    report = {
        "ok": ok_all,
        "checked_at": now.isoformat(),
        "metrics": {
            "snapshot_age_sec": round(snapshot_age, 2),
            "card_age_sec": round(card_age, 2),
            "market_cache_age_sec": round(market_cache_age, 2),
            "events_cache_age_sec": round(events_cache_age, 2),
            "scored_cards": len(scored_cards),
            "poly_linked_cards": len(poly_linked),
            "poly_linked_ratio": round(poly_ratio, 4),
            "wallet_present_cards": len(wallet_present),
            "wallet_present_ratio": round(wallet_ratio, 4),
            "coverage_untracked_event_count": int(coverage.get("untracked_event_count", 0)),
            "coverage_discovered_no_snapshots_count": len(discovered_no_snapshots),
            "coverage_discovered_no_snapshots_max_age_sec": round(max_discovered_no_snapshots_age, 2),
        },
        "checks": checks,
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=True))
    else:
        print("Health Check")
        print(f"- snapshot age: {_status(checks['snapshot_freshness'])} ({report['metrics']['snapshot_age_sec']}s)")
        print(f"- action card age: {_status(checks['action_card_freshness'])} ({report['metrics']['card_age_sec']}s)")
        print(
            f"- market cache age: {_status(checks['market_cache_freshness'])} "
            f"({report['metrics']['market_cache_age_sec']}s)"
        )
        print(
            f"- events cache age: {_status(checks['events_cache_freshness'])} "
            f"({report['metrics']['events_cache_age_sec']}s)"
        )
        print(
            f"- coverage untracked events: {_status(checks['coverage_untracked_events'])} "
            f"({report['metrics']['coverage_untracked_event_count']})"
        )
        print(
            f"- discovered/no-snapshots count: {_status(checks['coverage_discovered_no_snapshots_count'])} "
            f"({report['metrics']['coverage_discovered_no_snapshots_count']})"
        )
        print(
            f"- discovered/no-snapshots age: {_status(checks['coverage_discovered_no_snapshots_age'])} "
            f"({report['metrics']['coverage_discovered_no_snapshots_max_age_sec']}s)"
        )
        print(
            f"- poly linked ratio: {_status(checks['poly_signal_completeness'])} "
            f"({report['metrics']['poly_linked_ratio']:.2%})"
        )
        print(
            f"- wallet signal ratio: {_status(checks['wallet_signal_presence'])} "
            f"({report['metrics']['wallet_present_ratio']:.2%})"
        )
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
