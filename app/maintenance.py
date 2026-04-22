from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class MaintenanceTask:
    name: str
    script_path: Path
    interval_sec: float
    timeout_sec: float = 600.0
    requires_env_var: str | None = None
    args: list[str] = field(default_factory=list)

    def enabled(self) -> bool:
        if self.interval_sec <= 0:
            return False
        if self.requires_env_var:
            return bool(os.getenv(self.requires_env_var, "").strip())
        return True


@dataclass
class MaintenanceRunner:
    repo_root: Path
    tasks: list[MaintenanceTask]
    _next_due_monotonic: dict[str, float] = field(default_factory=dict, repr=False)

    def run_due(self) -> None:
        now = time.monotonic()
        for task in self.tasks:
            if not task.enabled():
                continue
            due = self._next_due_monotonic.get(task.name, now)
            if now < due:
                continue
            self._run_task(task)
            self._next_due_monotonic[task.name] = time.monotonic() + task.interval_sec

    # Set of task names that write to the SQLite DB.  After these tasks finish,
    # we issue a WAL checkpoint so the main DB file stays small and the WAL
    # never grows to a size that causes lock contention or corruption.
    _DB_WRITING_TASKS: frozenset[str] = frozenset({
        "prune_data",
        "archive_bet_decisions",
        "record_outcomes",
        "fetch_nba_schedule",
    })

    def _run_task(self, task: MaintenanceTask) -> None:
        # Use the venv Python if available — avoids the -S flag issue where
        # the system Python launched via the watchdog skips site-packages.
        venv_python = self.repo_root / ".venv" / "bin" / "python3"
        python_exe = str(venv_python) if venv_python.exists() else sys.executable
        cmd = [python_exe, str(task.script_path)] + (task.args or [])
        env = os.environ.copy()
        extra_paths = [str(self.repo_root)]
        venv_sp = self.repo_root / ".venv" / "lib" / "python3.9" / "site-packages"
        if venv_sp.exists():
            extra_paths.append(str(venv_sp))
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(extra_paths + ([existing] if existing else []))
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.repo_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=task.timeout_sec,
                check=False,
            )
        except Exception as exc:
            logger.warning("Maintenance task failed to launch: %s (%s)", task.name, exc)
            return

        if proc.returncode == 0:
            logger.info("Maintenance task ok: %s", task.name)
            if proc.stdout.strip():
                logger.debug("Task output (%s): %s", task.name, proc.stdout.strip())
        else:
            logger.warning(
                "Maintenance task failed: %s rc=%s stderr=%s",
                task.name,
                proc.returncode,
                proc.stderr.strip() if proc.stderr else "",
            )

        # After any task that writes to the DB, checkpoint the WAL so new frames
        # are merged into the main file and the WAL stays small.
        if task.name in self._DB_WRITING_TASKS:
            self._checkpoint_wal()

    def _checkpoint_wal(self) -> None:
        """No-op: checkpointing is exclusively owned by the long-running runner process.
        Scripts with wal_autocheckpoint=0 never checkpoint — the runner's
        wal_autocheckpoint=500 handles it safely without races."""


def build_default_maintenance_tasks(
    *,
    repo_root: Path,
    fetch_markets_interval_sec: float,
    fetch_poly_interval_sec: float,
    fetch_wallet_flow_interval_sec: float,
    fetch_news_interval_sec: float,
    fetch_x_interval_sec: float,
    fetch_outcomes_interval_sec: float,
    record_outcomes_interval_sec: float,
) -> list[MaintenanceTask]:
    scripts_dir = repo_root / "scripts"
    return [
        MaintenanceTask(
            name="fetch_markets",
            script_path=scripts_dir / "fetch_markets.py",
            interval_sec=fetch_markets_interval_sec,
        ),
        # Fast poll for same-day specific-event series (KXPRESMENTION, KXTRUMPMENTION).
        # Runs every 5 minutes so new events appear on the dashboard quickly.
        MaintenanceTask(
            name="fetch_hot_events",
            script_path=scripts_dir / "fetch_hot_events.py",
            interval_sec=300.0,
            timeout_sec=60.0,
        ),
        # Live settlement monitor — polls for same-day settled markets every 3 minutes
        # so the scoring engine can apply event-active and settled-phrase signals.
        MaintenanceTask(
            name="fetch_settlements",
            script_path=scripts_dir / "fetch_settlements.py",
            interval_sec=180.0,
            timeout_sec=45.0,
        ),
        MaintenanceTask(
            name="fetch_polymarket",
            script_path=scripts_dir / "fetch_polymarket.py",
            interval_sec=fetch_poly_interval_sec,
        ),
        MaintenanceTask(
            name="fetch_wallet_flow",
            script_path=scripts_dir / "fetch_wallet_flow.py",
            interval_sec=fetch_wallet_flow_interval_sec,
        ),
        MaintenanceTask(
            name="fetch_news_signals",
            script_path=scripts_dir / "fetch_news_signals.py",
            interval_sec=fetch_news_interval_sec,
        ),
        # Process Trump's Truth Social posts written by OpenClaw into signals.yaml.
        # Runs every 15 minutes. Only meaningful when OpenClaw has recently written
        # fresh posts to data/truth_social_posts.json.
        MaintenanceTask(
            name="process_truth_social",
            script_path=scripts_dir / "process_truth_social.py",
            interval_sec=900.0,
            timeout_sec=30.0,
        ),
        MaintenanceTask(
            name="fetch_x_signals",
            script_path=scripts_dir / "fetch_x_signals.py",
            interval_sec=fetch_x_interval_sec,
            requires_env_var="X_BEARER_TOKEN",
        ),
        MaintenanceTask(
            name="fetch_outcomes",
            script_path=scripts_dir / "fetch_outcomes.py",
            interval_sec=fetch_outcomes_interval_sec,
        ),
        # Archive the best BUY decision per market into bet_journal BEFORE
        # prune_snapshots wipes action_cards.  Runs every 6 hours to ensure
        # no bet decisions are lost when markets resolve 7-31 days later.
        MaintenanceTask(
            name="archive_bet_decisions",
            script_path=scripts_dir / "archive_bet_decisions.py",
            interval_sec=21600.0,  # every 6 hours
            timeout_sec=30.0,
        ),
        MaintenanceTask(
            name="record_outcomes",
            script_path=scripts_dir / "record_outcomes.py",
            interval_sec=record_outcomes_interval_sec,
        ),
        # Post-event postmortem: detects events resolved in the last 4h,
        # computes win rate / P&L / drift vs baseline, logs to
        # data/logs/postmortem_YYYYMMDD.log, and writes data/drift_alerts.json.
        # Runs every 30 min so a report is ready within half an hour of close.
        MaintenanceTask(
            name="post_event_summary",
            script_path=scripts_dir / "post_event_summary.py",
            interval_sec=1800.0,
            timeout_sec=30.0,
        ),
        # Refresh the resolved Kalshi outcomes cache once per day, then
        # immediately re-calibrate base rates so p_literal stays accurate.
        MaintenanceTask(
            name="refresh_outcomes",
            script_path=scripts_dir / "fetch_outcomes.py",
            interval_sec=86400.0,
            timeout_sec=300.0,
        ),
        # Auto-ingest any new corpus transcript files dropped into data/corpus/.
        # When OpenClaw writes a new transcript, this picks it up within 30 minutes
        # so the live phrase-hit engine can use it immediately.  Runs hourly since
        # new transcripts arrive infrequently.
        MaintenanceTask(
            name="ingest_corpus",
            script_path=scripts_dir / "ingest_corpus.py",
            interval_sec=3600.0,
            timeout_sec=120.0,
        ),
        # Auto-discover new Rev.com transcripts for corpus speakers (Trump default),
        # extract speaker text, write corpus files, and run ingest_corpus.
        # Runs once per day.  Requires BRAVE_API_KEY for best URL discovery; falls
        # back to Rev RSS and HTML scrape if the key is absent.
        MaintenanceTask(
            name="auto_ingest_corpus",
            script_path=scripts_dir / "auto_ingest_corpus.py",
            interval_sec=86400.0,
            timeout_sec=300.0,
        ),
        MaintenanceTask(
            name="calibrate_base_rates",
            script_path=scripts_dir / "calibrate_base_rates.py",
            interval_sec=86400.0,
            timeout_sec=120.0,
        ),
        # Daily outcome report — runs after calibration so the log captures
        # a full 30-day rolling window.  Output goes to stdout which
        # MaintenanceRunner captures in the application log.
        MaintenanceTask(
            name="report_outcomes",
            script_path=scripts_dir / "report_outcomes.py",
            interval_sec=86400.0,
            timeout_sec=30.0,
        ),
        # Recompute rolling N-speech hit rates daily alongside calibration.
        # Produces data/rolling_hit_rates.json — picked up by ScoringEngine
        # on its next outcomes refresh cycle (every 15 min).
        MaintenanceTask(
            name="compute_rolling_rates",
            script_path=scripts_dir / "compute_rolling_rates.py",
            interval_sec=86400.0,
            timeout_sec=60.0,
        ),
        # Recompute phrase co-occurrence index daily.
        # Produces data/phrase_cooccurrence.json — picked up by LiveSettlements
        # (loaded in ScoringEngine.from_cache()). When a phrase settles YES in a
        # live event, co-occurring phrases get a COOCCUR_BOOST p_literal adjustment.
        # Now uses recency-weighted counts (halflife=120 days) so stale patterns decay.
        MaintenanceTask(
            name="compute_cooccurrence",
            script_path=scripts_dir / "compute_cooccurrence.py",
            interval_sec=86400.0,
            timeout_sec=60.0,
        ),
        # Compute per-phrase 30d vs 90d YES-rate trend flags.  Writes
        # data/phrase_trends.json — picked up by PhraseTrendCache in scorer.
        MaintenanceTask(
            name="compute_phrase_trends",
            script_path=scripts_dir / "compute_phrase_trends.py",
            interval_sec=86400.0,
            timeout_sec=60.0,
        ),
        # Compute empirical base rates from resolved Kalshi outcomes and write
        # config/base_rates_auto.yaml.  Layered on top of manual base_rates.yaml
        # so statistically-grounded rates override manual guesses (N >= 8).
        MaintenanceTask(
            name="compute_base_rates",
            script_path=scripts_dir / "compute_base_rates.py",
            interval_sec=14400.0,  # every 4h — recalibrate as outcomes accumulate
            timeout_sec=60.0,
        ),
        # Compute market-maker bias map from resolved outcomes.
        # Writes data/bias_map.json — loaded by BiasMapCache in scorer as the
        # highest-priority base rate source (series + phrase specific, empirical).
        MaintenanceTask(
            name="compute_bias_map",
            script_path=scripts_dir / "compute_bias_map.py",
            interval_sec=14400.0,  # every 4h
            timeout_sec=60.0,
        ),
        # Compute empirical hazard rates from corpus transcripts.
        # Writes data/phrase_hazard_rates.json — loaded by PhraseHazardCache
        # to replace static time_decay with phrase-specific survival probabilities.
        # Runs daily after corpus ingestion to pick up new transcripts.
        MaintenanceTask(
            name="compute_hazard_rates",
            script_path=scripts_dir / "compute_hazard_rates.py",
            interval_sec=86400.0,  # daily  
            timeout_sec=120.0,     # can be slow with large corpus
        ),
        # ── META-LEARNING LAYER ────────────────────────────────────────────
        # Adaptive signal weights: learns which signals help/hurt per speaker
        # from outcome data. Writes data/signal_weights.json. Every 4h.
        MaintenanceTask(
            name="compute_signal_weights",
            script_path=scripts_dir / "compute_signal_weights.py",
            interval_sec=14400.0,
            timeout_sec=60.0,
        ),
        # Bayesian base rates: Beta-Bernoulli posteriors with uncertainty.
        # Writes data/bayesian_rates.json. Every 4h alongside base_rates.
        MaintenanceTask(
            name="compute_bayesian_rates",
            script_path=scripts_dir / "compute_bayesian_rates.py",
            interval_sec=14400.0,
            timeout_sec=120.0,
        ),
        # Cross-phrase correlation matrix for portfolio concentration.
        # Writes data/phrase_correlations.json. Daily (correlations are stable).
        MaintenanceTask(
            name="compute_phrase_correlations",
            script_path=scripts_dir / "compute_phrase_correlations.py",
            interval_sec=86400.0,
            timeout_sec=120.0,
        ),
        # Scan for systematically losing signal patterns (P4.1 regime detection).
        # Writes data/regime_alerts.json — read by dashboard for operator warnings.
        # Runs daily; uses 30-day rolling window with min 10 bets per pattern.
        MaintenanceTask(
            name="detect_regimes",
            script_path=scripts_dir / "detect_regimes.py",
            interval_sec=86400.0,  # daily
            timeout_sec=60.0,
        ),
        # Grid search over EV/Kelly/confidence thresholds on historical outcomes.
        # Writes data/threshold_optimization.json — read by dashboard System tab.
        # Runs weekly (thresholds are stable; only need periodic re-evaluation).
        MaintenanceTask(
            name="optimize_thresholds",
            script_path=scripts_dir / "optimize_thresholds.py",
            interval_sec=604800.0,  # weekly
            timeout_sec=120.0,
        ),
        # Fetch new Fed FOMC press conference transcripts (PDF → text).
        # Runs weekly; discovers new FOMC dates automatically.  Writes to
        # data/corpus/powell/ and runs ingest_corpus.
        MaintenanceTask(
            name="fetch_fed_transcripts",
            script_path=scripts_dir / "fetch_fed_transcripts.py",
            interval_sec=604800.0,  # weekly
            timeout_sec=300.0,
        ),
        # (compute_cooccurrence is registered once above — duplicate task removed)
        # Compute Brier Skill Score vs market mid.  Runs daily — writes
        # data/bss_metrics.json which the dashboard System tab reads.
        # Uses --save flag to run all three modes (standard, live, walk-forward)
        # and persist results. Fast (< 30s) so daily cadence is sufficient.
        MaintenanceTask(
            name="compute_bss",
            script_path=scripts_dir / "backtest_outcomes.py",
            args=["--save"],
            interval_sec=86400.0,
            timeout_sec=60.0,
        ),
        # Produces data/price_velocity.json — picked up by ScoringEngine's
        # PriceVelocityCache (stale after 10 min).  Runs every 5 min so the
        # in-memory cache is always populated on the next scoring cycle.
        MaintenanceTask(
            name="compute_price_velocity",
            script_path=scripts_dir / "compute_price_velocity.py",
            interval_sec=300.0,
            timeout_sec=30.0,
        ),
        # Fetches WH official feed + Google News for signing/remarks/briefing
        # announcements.  Produces data/wh_schedule.json which is used by
        # WHScheduleCache to override event_type and boost topic-relevant phrases.
        # Runs every 30 min so we have fresh context within half an hour of any
        # new signing or remarks announcement.
        MaintenanceTask(
            name="fetch_wh_schedule",
            script_path=scripts_dir / "fetch_wh_schedule.py",
            interval_sec=1800.0,
            timeout_sec=30.0,
        ),
        # Prune every hour with 1-day retention.  Runs frequently so the DB
        # never gets large enough to cause I/O lock contention.  The new
        # incremental batch-delete approach always makes forward progress
        # even on a large DB (no full COUNT(*) scans, 100K rows per batch).
        MaintenanceTask(
            name="prune_data",
            script_path=scripts_dir / "prune_snapshots.py",
            interval_sec=900.0,   # 15 min — catches growth before it compounds
            timeout_sec=1200.0,   # 20 min — plenty even for a 2 GB DB
        ),
        # D1: Signal aggregation — WH RSS, Google News, Truth Social, Google Trends.
        # Produces data/signal_context.json consumed by the LLM reasoning step.
        # Runs every 50 min — slightly ahead of the hourly LLM tasks so context is fresh.
        MaintenanceTask(
            name="fetch_signals",
            script_path=scripts_dir / "fetch_signals.py",
            interval_sec=3000.0,   # 50 min
            timeout_sec=60.0,
        ),
        # D2: LLM reasoning — GPT-4o-mini phrase probability adjustments.
        # Reads signal_context.json, writes llm_boost to data/signals.yaml.
        # Only runs when OPENAI_API_KEY is set (skipped otherwise).
        # Runs every 60 min — hourly cadence to stay within API budget.
        MaintenanceTask(
            name="analyze_signals",
            script_path=scripts_dir / "analyze_signals.py",
            interval_sec=3600.0,   # 60 min
            timeout_sec=1200.0,    # 20 min — LLM batching can take 10-15 min for 300 phrases
            requires_env_var="OPENAI_API_KEY",
        ),
        # D3: Per-event LLM scoring — GPT-4o event-specific phrase adjustments.
        # Produces data/event_signals/{event_id}.json for each live/scheduled event.
        # Only runs when OPENAI_API_KEY is set (skipped otherwise).
        # Runs every 60 min — hourly cadence to stay within API budget.
        MaintenanceTask(
            name="analyze_event",
            script_path=scripts_dir / "analyze_event.py",
            interval_sec=7200.0,   # 120 min (was 60; freshness TTL is 6h so hourly was wasted)
            timeout_sec=600.0,     # 10 min — processes political events only (sports skipped)
            requires_env_var="OPENAI_API_KEY",
        ),
        # D1c: Event-title certainty injector — maps event agenda to p_overrides.
        # Parses event titles (bill names, person names, country names) and writes
        # p_overrides / p_floors into active event signal files.
        # This is the primary edge source: "Trump signing SAVE AMERICA ACT" means
        # the phrase "save america act" is 92%+ certain — buy YES at market open.
        # Runs every 10 min — cheap, deterministic, no API calls.
        # Runs BEFORE extract_ts_phrases so TS floors only upgrade, never downgrade,
        # event-title overrides.
        MaintenanceTask(
            name="extract_event_certainties",
            script_path=scripts_dir / "extract_event_certainties.py",
            interval_sec=600.0,    # 10 min
            timeout_sec=30.0,
        ),
        # D1b: Truth Social phrase injector — causal pre-event p_floor injection.
        # Scans truth_social_posts.json for verbatim phrase matches and writes
        # p_floor values into active event signal files (trump/leavitt/hegseth only).
        # If Trump posted "SAVE AMERICA ACT" 12h ago → p_floor=0.88 for that phrase.
        # Runs every 15 min — cheap (no API calls), high-value for pre-event bets.
        # Runs AFTER analyze_event so LLM p_floors are already present; this script
        # only upgrades floors, never downgrades them.
        MaintenanceTask(
            name="extract_ts_phrases",
            script_path=scripts_dir / "extract_ts_phrases.py",
            interval_sec=900.0,    # 15 min
            timeout_sec=30.0,
        ),
        # E1: NBA schedule seeder — parses KXNBAMENTION market IDs, seeds events into
        # edge.db, writes data/nba_schedule.json with home team → arena mapping.
        # Runs every 30 min (cheap, deterministic, ~100ms).
        MaintenanceTask(
            name="fetch_nba_schedule",
            script_path=scripts_dir / "fetch_nba_schedule.py",
            interval_sec=1800.0,   # 30 min
            timeout_sec=30.0,
        ),
        # E2: NBA certainty injector — writes arena/sponsor p_overrides (~92%) and
        # universal-phrase p_floors into data/event_signals/nba:<ticker>.json.
        # Runs every 30 min, after fetch_nba_schedule (guaranteed by ordering here).
        MaintenanceTask(
            name="extract_nba_certainties",
            script_path=scripts_dir / "extract_nba_certainties.py",
            interval_sec=1800.0,   # 30 min
            timeout_sec=30.0,
        ),
        # E3: MLB certainty injector — writes ballpark p_overrides (~90%) and
        # universal-phrase p_floors for all active MLB mention markets.
        # Runs every 30 min; no schedule API needed (reads directly from DB).
        MaintenanceTask(
            name="extract_mlb_certainties",
            script_path=scripts_dir / "extract_mlb_certainties.py",
            interval_sec=1800.0,   # 30 min
            timeout_sec=30.0,
        ),
        # E4: NCAAB certainty injector — writes empirically-calibrated p_floors
        # (from 100 resolved outcomes) for all active NCAAB mention markets.
        # No arena overrides (Kalshi NCAAB markets don't include venue names).
        MaintenanceTask(
            name="extract_ncaab_certainties",
            script_path=scripts_dir / "extract_ncaab_certainties.py",
            interval_sec=1800.0,   # 30 min
            timeout_sec=30.0,
        ),
    ]
