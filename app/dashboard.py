#!/usr/bin/env python3
"""Kalshi Mention Edge Dashboard — local web UI for trading decisions.

Usage:
    python3 -m app.dashboard          # opens http://localhost:8777
    python3 -m app.dashboard --port 9000

No extra dependencies — uses Python's built-in http.server + SQLite.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.db import connect

logger = logging.getLogger(__name__)

# Repo root (dashboard must find data/ even if cwd is not the project directory)
_REPO_ROOT = Path(__file__).resolve().parent.parent

DB_PATH = Path("data/edge.db")
KALSHI_CACHE = Path("data/kalshi_markets.json")
OUTCOMES_CACHE = Path("data/kalshi_outcomes.json")
EVENTS_CACHE = Path("data/kalshi_events.json")
LLM_ANALYSIS_CACHE = Path("data/llm_analysis.json")
DEFAULT_PORT = 8777

# ── API response cache (avoids repeated slow json_extract full-table scans) ───
_API_CACHE: dict = {}            # {"body": bytes, "ts": float, "ver": int}
_API_CACHE_TTL = 15              # seconds — refresh every 15s max
_API_SCHEMA_VERSION = 8          # bump when /api/data payload shape changes (invalidates cache)

# Mention-market speakers shown on Performance.
PERFORMANCE_SPEAKER_ORDER: tuple[str, ...] = (
    "trump", "leavitt", "mamdani", "fed",
    "ncaab", "nba", "mlb", "mma",
)

_PHRASE_TREND_SPEAKER_ALIASES: dict[str, str] = {
    "powell": "fed",
}

# ── Script runner ─────────────────────────────────────────────────────────────
_SCRIPT_JOBS: dict[str, dict] = {}  # job_id → {status, output, exit_code, ...}

SCRIPT_REGISTRY: list[dict] = [
    # Pre-Event — run these before every event, in order, to inject causal certainties
    {"id": "event_certainties", "script": "scripts/extract_event_certainties.py", "label": "Event Certainties",  "desc": "Parse event title (bill names, people, countries) → p_overrides. Run first — highest-value pre-event signal.", "group": "Pre-Event", "tag": "preevent"},
    {"id": "ts_phrases",        "script": "scripts/extract_ts_phrases.py",        "label": "Truth Social Floors","desc": "Match Trump's recent posts against active phrases → p_floors. Run after Event Certainties.", "group": "Pre-Event", "tag": "preevent"},
    # Basketball, Baseball, College Basketball mention markets
    {"id": "nba_schedule",      "script": "scripts/fetch_nba_schedule.py",       "label": "NBA Schedule",        "desc": "Fetch today's NBA game schedule, seed events into DB. Run before NBA Certainties.", "group": "Sports", "tag": "nba"},
    {"id": "nba_certainties",   "script": "scripts/extract_nba_certainties.py",  "label": "NBA Certainties",     "desc": "Inject arena/sponsor p_overrides (~92%) and universal phrase p_floors for all NBA games.", "group": "Sports", "tag": "nba"},
    {"id": "mlb_certainties",   "script": "scripts/extract_mlb_certainties.py",  "label": "MLB Certainties",     "desc": "Inject ballpark p_overrides (~90%) and universal phrase floors for active MLB games. Run after Fetch Markets.", "group": "Sports", "tag": "nba"},
    {"id": "ncaab_certainties", "script": "scripts/extract_ncaab_certainties.py","label": "NCAAB Certainties",   "desc": "Inject empirical phrase floors for active NCAAB mention markets based on resolved outcomes.", "group": "Sports", "tag": "nba"},
    # Data Fetching — run before AI Intelligence to ensure fresh inputs
    {"id": "fetch_markets",     "script": "scripts/fetch_markets.py",      "label": "Fetch Markets",           "desc": "Pull latest mention markets from the Kalshi API and update local cache",     "group": "Data Fetching",   "tag": "data"},
    {"id": "fetch_outcomes",    "script": "scripts/fetch_outcomes.py",     "label": "Fetch Outcomes",          "desc": "Download finalized market outcomes from Kalshi for calibration",             "group": "Data Fetching",   "tag": "data"},
    {"id": "fetch_polymarket",  "script": "scripts/fetch_polymarket.py",   "label": "Fetch Polymarket",        "desc": "Sync Polymarket prices and match to Kalshi mention markets",                  "group": "Data Fetching",   "tag": "data"},
    {"id": "fetch_wh",          "script": "scripts/fetch_wh_schedule.py",  "label": "WH Schedule",             "desc": "Fetch the White House daily schedule for event context and planning",         "group": "Data Fetching",   "tag": "data"},
    {"id": "fetch_signals",     "script": "scripts/fetch_signals.py",      "label": "Fetch Signals",           "desc": "Aggregate signal data (news, buzz, X) for the LLM reasoning pass",           "group": "Data Fetching",   "tag": "data"},
    {"id": "fetch_news",        "script": "scripts/fetch_news_signals.py", "label": "Fetch News",              "desc": "Pull latest news signals for phrase relevance scoring",                      "group": "Data Fetching",   "tag": "data"},
    {"id": "fetch_x",           "script": "scripts/fetch_x_signals.py",    "label": "Fetch X Signals",         "desc": "Collect X/Twitter watchlist signals for monitored accounts",                  "group": "Data Fetching",   "tag": "data"},
    {"id": "fetch_wallet",      "script": "scripts/fetch_wallet_flow.py",  "label": "Wallet Flow",             "desc": "Build alpha signals from public Polymarket wallet trade activity",            "group": "Data Fetching",   "tag": "data"},
    {"id": "fetch_settlements", "script": "scripts/fetch_settlements.py",  "label": "Fetch Settlements",       "desc": "Pull recently settled Kalshi markets for same-event boosting signals",        "group": "Data Fetching",   "tag": "data"},
    {"id": "fetch_hot",         "script": "scripts/fetch_hot_events.py",   "label": "Hot Events",              "desc": "Fast-fetch same-day specific-event markets for rapid response",              "group": "Data Fetching",   "tag": "data"},
    {"id": "fetch_fed",         "script": "scripts/fetch_fed_transcripts.py","label": "Fed Transcripts",       "desc": "Scrape latest Fed/Powell speech transcripts into the corpus for Powell market scoring.", "group": "Data Fetching", "tag": "data"},
    # AI Intelligence — run after Data Fetching
    {"id": "analyze_event",     "script": "scripts/analyze_event.py",     "label": "Analyze Event Signals",   "desc": "Generate per-event LLM phrase multipliers for all active events",           "group": "AI Intelligence", "tag": "LLM"},
    {"id": "analyze_signals",   "script": "scripts/analyze_signals.py",   "label": "Analyze Global Signals",  "desc": "Run the LLM reasoning layer to score phrase probability adjustments",         "group": "AI Intelligence", "tag": "LLM"},
    # Backtesting
    {"id": "backtest_outcomes",  "script": "scripts/backtest_outcomes.py",  "label": "Historical Backtest",     "desc": "Backtest the scoring model against all resolved Kalshi mention-market outcomes", "group": "Backtesting",     "tag": "backtest"},
    {"id": "backtest_scorecards","script": "scripts/backtest_scorecards.py","label": "Scorecards Backtest",     "desc": "Simulate model performance on recent live scorecards",                       "group": "Backtesting",     "tag": "backtest"},
    {"id": "backtest_llm",       "script": "scripts/backtest_llm_impact.py","label": "LLM Impact Analysis",    "desc": "Quantify the LLM layer's contribution to historical prediction accuracy",    "group": "Backtesting",     "tag": "backtest"},
    # Calibration — run in this order: raw rates → bias/hazard → platt → velocity → regime → thresholds → policy
    {"id": "rolling_rates",     "script": "scripts/compute_rolling_rates.py","label": "Rolling Rates",         "desc": "1. Compute recency-weighted phrase hit rates from resolved outcomes",          "group": "Calibration",     "tag": "calib"},
    {"id": "cooccurrence",      "script": "scripts/compute_cooccurrence.py","label": "Co-occurrence Index",    "desc": "2. Build phrase co-occurrence index from historical outcome data",             "group": "Calibration",     "tag": "calib"},
    {"id": "compute_base",      "script": "scripts/compute_base_rates.py", "label": "Compute Base Rates",     "desc": "3. Derive per-phrase base rates from resolved outcomes. Must run before Calibrate Base Rates.", "group": "Calibration", "tag": "calib"},
    {"id": "bias_map",          "script": "scripts/compute_bias_map.py",   "label": "Compute Bias Map",       "desc": "4. Build BIAS_MAP_OVERPRICED/UNDERPRICED signals from systematic market mispricing patterns.", "group": "Calibration", "tag": "calib"},
    {"id": "hazard_rates",      "script": "scripts/compute_hazard_rates.py","label": "Compute Hazard Rates",  "desc": "5. Derive phrase-specific time-decay survival curves from corpus (replaces static decay).", "group": "Calibration", "tag": "calib"},
    {"id": "phrase_trends",     "script": "scripts/compute_phrase_trends.py","label": "Phrase Trends",        "desc": "6. Compute 30d vs 90d YES-rate trends — flags VOCAB_TRENDING_UP/DOWN signals", "group": "Calibration",     "tag": "calib"},
    {"id": "calibrate_base",    "script": "scripts/calibrate_base_rates.py","label": "Calibrate Base Rates",  "desc": "7. Refit Platt scaling parameters from resolved outcomes. Run after Compute Base Rates.", "group": "Calibration", "tag": "calib"},
    {"id": "price_velocity",    "script": "scripts/compute_price_velocity.py","label": "Price Velocity",      "desc": "8. Calculate momentum signals across active Kalshi markets",                   "group": "Calibration",     "tag": "calib"},
    {"id": "detect_regimes",    "script": "scripts/detect_regimes.py",     "label": "Detect Regimes",         "desc": "9. Scan outcomes for systematically losing signal patterns; writes regime_alerts.json.", "group": "Calibration", "tag": "calib"},
    {"id": "optimize_thresh",   "script": "scripts/optimize_thresholds.py","label": "Optimize Thresholds",    "desc": "10. Grid search EV/Kelly/confidence thresholds on historical outcomes; writes threshold_optimization.json.", "group": "Calibration", "tag": "calib"},
    {"id": "tune_policy",       "script": "scripts/tune_policy.py",        "label": "Tune Policy",            "desc": "11. Apply optimized thresholds — run last after all calibration steps complete.", "group": "Calibration",    "tag": "calib"},
    # Health & Reporting
    {"id": "health_check",      "script": "scripts/health_check.py",       "label": "Health Check",            "desc": "Run event-day freshness, coverage, and signal completeness checks",          "group": "Health & Reporting","tag": "health"},
    {"id": "report_outcomes",   "script": "scripts/report_outcomes.py",    "label": "Report Outcomes",         "desc": "Generate a performance summary of recent betting outcomes and P&L",          "group": "Health & Reporting","tag": "health"},
    {"id": "post_event",        "script": "scripts/post_event_summary.py", "label": "Post-Event Summary",      "desc": "Automated postmortem and drift detector for completed events",               "group": "Health & Reporting","tag": "health"},
    {"id": "record_outcomes",   "script": "scripts/record_outcomes.py",    "label": "Record Outcomes",         "desc": "Record bet outcomes into the database for calibration tracking",             "group": "Health & Reporting","tag": "health"},
    {"id": "prune_snapshots",   "script": "scripts/prune_snapshots.py",    "label": "Prune Snapshots",         "desc": "Clean up stale market snapshots and rotate JSONL log files",                 "group": "Health & Reporting","tag": "health"},
    {"id": "archive_bets",      "script": "scripts/archive_bet_decisions.py","label": "Archive Bet Decisions", "desc": "Archive resolved action cards and bet decisions to long-term storage",       "group": "Health & Reporting","tag": "health"},
    {"id": "audit_series",      "script": "scripts/audit_mention_series.py","label": "Audit Mention Series",   "desc": "Compare fetch_markets vs fetch_outcomes series coverage; flags missing or mismatched tickers.", "group": "Health & Reporting","tag": "health"},
    # Corpus
    {"id": "ingest_corpus",     "script": "scripts/ingest_corpus.py",      "label": "Ingest Corpus",           "desc": "Bulk-load transcript files from data/corpus/ into the SQLite database",     "group": "Corpus",          "tag": "corpus"},
    {"id": "analyze_corpus",    "script": "scripts/analyze_corpus.py",     "label": "Analyze Corpus",          "desc": "Analyze scraped transcript data and calibrate base phrase rates",            "group": "Corpus",          "tag": "corpus"},
    {"id": "process_truth",     "script": "scripts/process_truth_social.py","label": "Process Truth Social",   "desc": "Process Trump Truth Social posts and update phrase signal boosts",          "group": "Corpus",          "tag": "corpus"},
    {"id": "scrape_corpus",     "script": "scripts/scrape_corpus.py",      "label": "Scrape Corpus",           "desc": "Scrape historical transcripts into the local corpus via Browser Relay",      "group": "Corpus",          "tag": "corpus"},
    # Engine control
    {"id": "start_engine",      "script": "scripts/start_engine.py",       "label": "Start Engine",            "desc": "Launch the scoring engine (runner + watcher + scorer + maintenance loop) as a background daemon. No-op if already running.", "group": "Engine Control",  "tag": "engine"},
    {"id": "stop_engine",       "script": "scripts/stop_engine.py",        "label": "Stop Engine",             "desc": "Gracefully stop the runner, watcher, ingestor, and scorer processes (SIGTERM then SIGKILL). Dashboard stays up.", "group": "Engine Control",  "tag": "danger"},
    {"id": "repair_system",     "script": "scripts/repair_system.py",      "label": "Repair System",           "desc": "One-click fix: stop engine → heal DB → ensure schema → restart engine → verify. Run this when anything is broken.", "group": "Engine Control",  "tag": "engine"},
]
_ALLOWED_SCRIPTS = {s["script"] for s in SCRIPT_REGISTRY}


def _run_script_job(job_id: str, script_path: str) -> None:
    """Run a script in a subprocess; stream output into the job store."""
    job = _SCRIPT_JOBS[job_id]
    env = os.environ.copy()
    try:
        env["PYTHONUNBUFFERED"] = "1"
        proc = subprocess.Popen(
            [sys.executable, "-u", script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=str(Path.cwd()),
            env=env,
        )
        job["pid"] = proc.pid
        assert proc.stdout is not None
        for line in proc.stdout:
            job["output"].append(line.rstrip("\n"))
            if len(job["output"]) > 3000:
                job["output"] = job["output"][-3000:]
        proc.wait(timeout=600)
        job["exit_code"] = proc.returncode
        job["status"] = "done" if proc.returncode == 0 else "error"
    except Exception as exc:
        job["output"].append(f"[RUNNER ERROR] {exc}")
        job["status"] = "error"
        job["exit_code"] = -1
    finally:
        job["ended_at"] = time.time()


# Series prefixes to exclude from the dashboard entirely.
# Only Trump, Leavitt, Mamdani, Fed, NBA, NCAAB, MLB, MMA are tracked.
_BLOCKED_SERIES_PREFIXES: tuple[str, ...] = (
    # Removed markets — not tracking
    "KXHOCHULMENTION",
    "KXNEWSOMMENTION",
    "KXSTARMERMENTIONB",
    "KXFOXNEWSMENTION",
    "KXLASTWORDMENTION",
    "KXLASTWORDCOUNT",
    "KXSNLMENTION",
    "KXMRBEASTMENTION",
    "KXWHPRESSBRIEFING",
    "KXCARNEYMENTION",
    "KXHOMANMENTION",
    "KXMENTION",
    "KXPERSONMENTION",
    "KXPOLITICSMENTION",
    "KXNYCMDEBMENTION",
    "KXMENTIONEARNDAL",
    "KXSURVIVORMENTION",
    "KXWBCMENTION",
    "KXENTMENTION",
    "KXEARNINGS",
    "KXJENSENMENTION",
    "KXTOPSONG",
    "KXMELANIAMENTION",
    # Duration markets — dedicated timing model needed
    "KXLEAVITTMENTIONDURATION",
    "KXTRUMPMENTIONDURATION",
)


def _is_blocked_market(market_id: str, series_ticker: str = "") -> bool:
    """Return True if this market should be hidden from the dashboard."""
    check = series_ticker.upper() or market_id.upper()
    return check.startswith(_BLOCKED_SERIES_PREFIXES)

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalshi Edge</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#09090b;--surface:#18181b;--surface2:#27272a;--surface3:#303035;--border:#3f3f46;--border2:#52525b;
  --text:#fafafa;--text2:#a1a1aa;--text3:#71717a;
  --green:#22c55e;--green-dim:rgba(34,197,94,.1);--green-mid:rgba(34,197,94,.22);
  --red:#ef4444;--red-dim:rgba(239,68,68,.1);--red-mid:rgba(239,68,68,.22);
  --blue:#3b82f6;--blue-dim:rgba(59,130,246,.1);
  --amber:#f59e0b;--amber-dim:rgba(245,158,11,.1);
  --purple:#a855f7;--purple-dim:rgba(168,85,247,.1);
  --r:12px;--rs:8px;--rx:6px;
}
html{font-family:'Inter',system-ui,sans-serif;-webkit-font-smoothing:antialiased}
body{background:var(--bg);color:var(--text);min-height:100vh}
.mono{font-family:'JetBrains Mono','SF Mono',monospace}

/* Shell */
.sh{max-width:1400px;margin:0 auto;padding:20px 24px 60px}

/* Header */
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:20px;flex-wrap:wrap;gap:12px}
.logo{display:flex;align-items:center;gap:10px}
.logo h1{font-size:20px;font-weight:700;letter-spacing:-.02em}
.logo .live{font-size:9px;font-weight:700;padding:3px 8px;border-radius:20px;
  background:var(--green-dim);color:var(--green);text-transform:uppercase;letter-spacing:.08em}
.ctrls{display:flex;align-items:center;gap:8px}
.ctrls select,.ctrls button{font-size:12px;font-weight:500;padding:6px 12px;border-radius:var(--rx);
  border:1px solid var(--border);background:var(--surface);color:var(--text);cursor:pointer;transition:all .15s}
.ctrls button:hover{border-color:var(--blue);background:var(--surface2)}
.ctrls .ts{font-size:11px;color:var(--text3);min-width:70px}

/* Stats */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:20px}
.st{background:var(--surface);border:1px solid var(--border);border-radius:var(--rs);padding:12px 14px}
.st .sl{font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.06em;margin-bottom:2px}
.st .sv{font-size:22px;font-weight:700;letter-spacing:-.02em}
.st .sv.g{color:var(--green)}.st .sv.r{color:var(--red)}.st .sv.b{color:var(--blue)}.st .sv.p{color:var(--purple)}.st .sv.a{color:var(--amber)}
.st .ss{font-size:10px;color:var(--text3);margin-top:1px}

/* Tabs */
.tabs{display:flex;gap:2px;margin-bottom:16px;border-bottom:1px solid var(--border);
  flex-wrap:nowrap;overflow-x:auto;max-width:100%;-webkit-overflow-scrolling:touch;scrollbar-width:thin}
.tab{padding:8px 12px;font-size:12px;font-weight:500;color:var(--text3);cursor:pointer;flex-shrink:0;white-space:nowrap;
  border-bottom:2px solid transparent;transition:all .12s;user-select:none}
.tab:hover{color:var(--text2)}
.tab.on{color:var(--text);border-bottom-color:var(--blue)}

/* Event group (accordion) */
.evt-group{margin-bottom:12px;border:1px solid var(--border);border-radius:var(--r);background:var(--surface);overflow:hidden}
.evt-head{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;cursor:pointer;
  user-select:none;transition:background .1s}
.evt-head:hover{background:var(--surface2)}
.evt-head .left{display:flex;align-items:center;gap:12px;flex:1;min-width:0}
.evt-head .speaker-av{width:32px;height:32px;border-radius:50%;display:flex;align-items:center;justify-content:center;
  font-weight:700;font-size:13px;text-transform:uppercase;flex-shrink:0}
.av-trump{background:var(--red-dim);color:var(--red)}
.av-leavitt{background:var(--blue-dim);color:var(--blue)}
.av-mamdani{background:var(--amber-dim);color:var(--amber)}
.av-fed{background:#1a2744;color:#5b9bd5}
.av-nba{background:rgba(249,115,22,.12);color:#f97316}
.av-ncaab{background:rgba(249,115,22,.12);color:#f97316}
.av-mlb{background:rgba(232,64,64,.12);color:#e84040}
.av-mma{background:rgba(192,132,252,.12);color:#c084fc}
/* ─── Speaker section wrapper (Markets tab) ─── */
.spk-section{margin-bottom:28px}
.spk-header{display:flex;align-items:center;gap:14px;padding:14px 20px 12px;background:var(--surface);border:1px solid var(--border);border-bottom:2px solid var(--border);border-radius:10px 10px 0 0;position:relative}
.spk-header::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;border-radius:10px 0 0 0;background:var(--spk-accent,var(--border2))}
.spk-av-lg{width:42px!important;height:42px!important;font-size:16px!important;flex-shrink:0}
.spk-info{flex:1;min-width:0}
.spk-name{font-size:15px;font-weight:700;color:var(--text1)}
.spk-meta{font-size:11px;color:var(--text3);display:flex;gap:10px;margin-top:3px;flex-wrap:wrap;align-items:center}
.spk-ev{font-family:'JetBrains Mono',monospace;font-weight:700;color:var(--green)}
.spk-badges{display:flex;gap:8px;align-items:center;flex-shrink:0}
.spk-body .evt-group{border-radius:0!important}
.spk-body .evt-group:last-child{border-radius:0 0 8px 8px!important}
/* Speaker accent bar colors */
.spk-trump{--spk-accent:var(--red)}
.spk-leavitt{--spk-accent:var(--blue)}
.spk-mamdani{--spk-accent:var(--amber)}
.spk-fed{--spk-accent:#5b9bd5}
.spk-nba{--spk-accent:#f97316}
.spk-ncaab{--spk-accent:#f97316}
.spk-mlb{--spk-accent:#e84040}
.spk-mma{--spk-accent:#c084fc}
.spk-other{--spk-accent:var(--border2)}
/* ─── Performance Intelligence page ─── */
.pi-hero{display:grid;grid-template-columns:repeat(5,1fr);border:1px solid var(--border2);border-radius:12px;overflow:hidden;margin-bottom:22px}
.pi-hero-cell{padding:20px 18px;border-right:1px solid var(--border2)}
.pi-hero-cell:last-child{border-right:none}
.pi-hero-num{font-size:26px;font-weight:800;line-height:1;margin-bottom:5px}
.pi-hero-lbl{font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.06em}
.pi-card{border:1px solid var(--border2);border-radius:12px;overflow:hidden;margin-bottom:20px}
.pi-card-head{padding:11px 18px;border-bottom:1px solid var(--border2);display:flex;align-items:center;justify-content:space-between;background:var(--surface2)}
.pi-card-title{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:var(--text2)}
.pi-sort{display:flex;gap:4px}
.pi-sort-btn{padding:3px 10px;border-radius:10px;border:1px solid var(--border2);background:transparent;color:var(--text3);font-size:11px;cursor:pointer;transition:all .12s}
.pi-sort-btn.on{background:var(--surface3);color:var(--text1);border-color:var(--border3)}
.pi-sort-btn:hover{border-color:var(--border3);color:var(--text2)}
.pi-spk-list{display:flex;flex-direction:column}
.pi-spk-row{padding:15px 20px 15px 24px;border-bottom:1px solid var(--border1);cursor:pointer;transition:background .12s;position:relative}
.pi-spk-row:last-child{border-bottom:none}
.pi-spk-row::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--spk-accent,var(--border2))}
.pi-spk-row:hover{background:var(--surface2)}
.pi-spk-row.pi-open{background:var(--surface2)}
.pi-spk-phrase-list{border-top:1px solid var(--border1);padding:10px 0 2px;margin-top:12px}
.pi-grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:700px){.pi-grid2{grid-template-columns:1fr}}
.pi-win-table{width:100%;border-collapse:collapse;font-size:13px}
.pi-win-table th{font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;padding:8px 14px;border-bottom:1px solid var(--border2);text-align:right;font-weight:500}
.pi-win-table th:first-child{text-align:left}
.pi-win-table td{padding:9px 14px;border-bottom:1px solid var(--border1);text-align:right}
.pi-win-table td:first-child{text-align:left;font-weight:600;color:var(--text2)}
.pi-win-table tbody tr:last-child td{border-bottom:none;border-top:2px solid var(--border2);color:var(--text1);font-weight:700}
.pi-sides{display:flex;gap:10px;padding:12px 14px;border-top:1px solid var(--border1)}
.pi-side-card{flex:1;background:var(--surface2);border-radius:8px;padding:10px 12px}
.pi-side-lbl{font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px}
.pi-side-wr{font-size:20px;font-weight:800;line-height:1}
.pi-side-sub{font-size:10px;color:var(--text3);margin-top:4px}
.pi-feed{display:flex;flex-direction:column;gap:2px;padding:8px 10px}
.pi-feed-row{display:flex;align-items:center;gap:7px;padding:6px 8px;border-radius:6px;transition:background .1s}
.pi-feed-row:hover{background:var(--surface3)}
.pi-feed-pill{font-size:10px;font-weight:700;padding:2px 7px;border-radius:8px;flex-shrink:0;min-width:36px;text-align:center}
.pi-pill-win{background:rgba(74,222,128,.12);color:#4ade80}
.pi-pill-loss{background:rgba(248,113,113,.12);color:#f87171}
.pi-feed-phrase{flex:1;min-width:0;font-size:12px;color:var(--text2);font-style:italic;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pi-feed-pnl{font-size:12px;font-weight:700;white-space:nowrap}
.pi-feed-time{font-size:10px;color:var(--text3);white-space:nowrap}
.pi-rank-bar{display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.pi-rank-sort{display:flex;gap:3px;flex-shrink:0}
.pi-rank-sort button{padding:4px 8px;border-radius:8px;border:1px solid var(--border2);background:transparent;color:var(--text3);font-size:10px;cursor:pointer}
.pi-rank-sort button.on{background:var(--surface3);color:var(--text1);border-color:var(--border3)}
.pi-rank-strip{display:flex;gap:8px;overflow-x:auto;padding:2px 0 12px;scroll-behavior:smooth;-webkit-overflow-scrolling:touch;scrollbar-width:thin}
.pi-rank-chip{flex:0 0 auto;width:108px;padding:8px 10px;border-radius:10px;border:1px solid var(--border2);background:var(--surface2);cursor:pointer;text-align:left;color:inherit;font:inherit}
.pi-rank-chip:hover{border-color:var(--border3);background:var(--surface3)}
.pi-rank-chip .rn{font-size:11px;font-weight:700;color:var(--text1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pi-rank-chip .rs{font-size:10px;color:var(--text3);margin-top:3px;line-height:1.35}
.pi-rank-chip.muted{opacity:.55}
.pi-vocab{margin-top:14px;padding-top:14px;border-top:1px solid var(--border1)}
.pi-vocab-empty{font-size:12px;color:var(--text3);font-style:italic;padding:8px 0}
.pi-vocab-table{width:100%;border-collapse:collapse;font-size:12px}
.pi-vocab-table th{font-size:9px;color:var(--text3);text-transform:uppercase;letter-spacing:.04em;padding:6px 8px;border-bottom:1px solid var(--border2);text-align:right;font-weight:500}
.pi-vocab-table th:first-child{text-align:left}
.pi-vocab-table td{padding:6px 8px;border-bottom:1px solid var(--border1);text-align:right}
.pi-vocab-table td:first-child{text-align:left;color:var(--text2);font-style:italic;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pi-perf-banner{font-size:12px;color:var(--amber);padding:10px 14px;background:var(--surface2);border:1px solid var(--border2);border-radius:10px;margin-bottom:14px}
.pi-perf-body{padding:14px 16px 20px;background:var(--surface);border:1px solid var(--border);border-top:none;border-radius:0 0 10px 10px}
.pi-perf-body .pi-grid2{margin-bottom:0}
.pi-perf-phrases{margin-top:14px;padding-top:14px;border-top:1px solid var(--border1)}
.pi-perf-phrases .pi-card-title{margin-bottom:8px;display:block}
.evt-head .info{min-width:0}
.evt-head .info h3{font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.evt-head .info .meta{font-size:11px;color:var(--text3);display:flex;gap:10px;margin-top:2px;flex-wrap:wrap}
.evt-head .right{display:flex;align-items:center;gap:12px;flex-shrink:0}
.evt-head .pill{font-size:11px;font-weight:600;padding:3px 10px;border-radius:20px}
.pill-yes{background:var(--green-dim);color:var(--green)}
.pill-no{background:var(--red-dim);color:var(--red)}
.pill-watch{background:var(--surface2);color:var(--text3)}
.pill-time{background:var(--surface2);color:var(--text2);font-weight:500}
.pill-ended{background:var(--red-dim);color:var(--red);font-weight:500}
.evt-group.closed{opacity:.45;border-color:var(--surface2)}
.evt-group.closed .evt-head:hover{opacity:.8}
.evt-head .chevron{color:var(--text3);font-size:18px;transition:transform .2s}
.evt-head.open .chevron{transform:rotate(180deg)}

.evt-body{display:none;border-top:1px solid var(--border)}
.evt-body.open{display:block}

/* ── Bet Cards (v2) ── */
.bet-cards{border-bottom:1px solid var(--border)}
.bet-card{display:flex;gap:20px;padding:18px 20px;border-bottom:1px solid rgba(63,63,70,.2);align-items:flex-start;transition:background .1s;position:relative}
.bet-card:last-child{border-bottom:none}
.bet-card:hover{background:rgba(255,255,255,.015)}
.bet-card.bc-yes{border-left:3px solid rgba(34,197,94,.5);padding-left:17px}
.bet-card.bc-no{border-left:3px solid rgba(239,68,68,.5);padding-left:17px}
.bet-card.bc-tier-a.bc-yes{background:rgba(34,197,94,.03);border-left-color:var(--green)}
.bet-card.bc-tier-a.bc-no{background:rgba(239,68,68,.03);border-left-color:var(--red)}
/* Left body */
.bc-body{flex:1;min-width:0;display:flex;flex-direction:column;gap:7px}
.bc-top{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.bc-side-badge{font-size:10px;font-weight:700;padding:3px 10px;border-radius:5px;text-transform:uppercase;letter-spacing:.05em;flex-shrink:0}
.bc-yes .bc-side-badge{background:var(--green-dim);color:var(--green);border:1px solid rgba(34,197,94,.2)}
.bc-no .bc-side-badge{background:var(--red-dim);color:var(--red);border:1px solid rgba(239,68,68,.2)}
.bc-phrase{font-size:18px;font-weight:700;letter-spacing:-.02em;color:var(--text)}
.bc-tags{display:flex;gap:5px;flex-wrap:wrap;align-items:center}
/* Prices pill row */
.bc-prices{display:inline-flex;gap:0;background:var(--surface2);border:1px solid var(--border);border-radius:var(--rs);overflow:hidden;align-self:flex-start}
.bc-price{display:flex;flex-direction:column;align-items:center;padding:6px 13px;border-right:1px solid var(--border)}
.bc-price:last-child{border-right:none}
.bc-pval{font-size:14px;font-weight:700;font-family:'JetBrains Mono',monospace;line-height:1.1}
.bc-plbl{font-size:8px;font-weight:600;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);margin-top:3px}
.bc-pval-model{color:var(--amber)}.bc-pval-poly{color:var(--purple)}
/* LLM inline panel */
.bc-llm{padding:8px 11px;border-radius:6px;border-left:2px solid}
.bc-llm-up{background:rgba(34,197,94,.05);border-color:var(--green)}
.bc-llm-dn{background:rgba(239,68,68,.05);border-color:var(--red)}
.bc-llm-hd{display:flex;align-items:center;gap:6px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
.bc-llm-up .bc-llm-hd{color:var(--green)}.bc-llm-dn .bc-llm-hd{color:var(--red)}
.bc-llm-pill{padding:1px 6px;border-radius:3px;font-size:10px;font-weight:800}
.bc-llm-up .bc-llm-pill{background:var(--green-mid);color:var(--green)}
.bc-llm-dn .bc-llm-pill{background:var(--red-mid);color:var(--red)}
.bc-llm-txt{color:var(--text2);font-size:11px;line-height:1.5}
.bc-llm-ev{font-style:italic;color:var(--text3);font-size:10px;margin-top:4px;border-top:1px solid rgba(255,255,255,.06);padding-top:4px}
/* Right column */
.bc-right{display:flex;flex-direction:column;align-items:flex-end;gap:12px;flex-shrink:0;min-width:130px}
.bc-ev-wrap{text-align:right}
.bc-ev-num{font-size:30px;font-weight:800;letter-spacing:-.03em;line-height:1}
.bc-ev-num.pos{color:var(--green)}.bc-ev-num.neg{color:var(--red)}
.bc-ev-lbl{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);margin-top:3px}
.bc-ev-kl{font-size:10px;color:var(--amber);font-weight:600;margin-top:3px}
/* Action button */
.bc-btn{padding:10px 18px;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;border:1px solid;transition:all .15s;letter-spacing:-.01em;white-space:nowrap;width:100%}
.bc-btn-yes{background:var(--green-dim);color:var(--green);border-color:rgba(34,197,94,.3)}
.bc-btn-yes:hover{background:rgba(34,197,94,.18);border-color:rgba(34,197,94,.55)}
.bc-btn-no{background:var(--red-dim);color:var(--red);border-color:rgba(239,68,68,.3)}
.bc-btn-no:hover{background:rgba(239,68,68,.18);border-color:rgba(239,68,68,.55)}
.bc-copy-lbl{font-size:9px;color:var(--text3);cursor:pointer;text-align:right;margin-top:2px}
.bc-copy-lbl:hover{color:var(--text2)}
/* Watch cards (compact list) */
.watch-list{padding:8px 0}
.watch-row{display:flex;align-items:center;gap:10px;padding:7px 20px;border-bottom:1px solid rgba(63,63,70,.15);font-size:12px}
.watch-row:last-child{border-bottom:none}
.watch-row:hover{background:rgba(255,255,255,.01)}
.wr-phrase{font-weight:600;font-size:13px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wr-tags{display:flex;gap:4px;flex-wrap:wrap;flex-shrink:0}
.wr-prices{display:flex;gap:8px;font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--text2);flex-shrink:0}
.wr-ev{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;flex-shrink:0;min-width:52px;text-align:right}
.wr-ev.pos{color:var(--green)}.wr-ev.neg{color:var(--text3)}
/* Legacy compat (renderAllCardsTable still uses these) */
.hint{font-size:13px;font-weight:700;padding:7px 14px;border-radius:var(--rx);display:inline-block;cursor:pointer;transition:opacity .15s;letter-spacing:-.01em}
.hint:hover{opacity:.8}
.hint-yes{background:var(--green-dim);color:var(--green);border:1px solid rgba(34,197,94,.25)}
.hint-no{background:var(--red-dim);color:var(--red);border:1px solid rgba(239,68,68,.25)}
.hint-copy{font-size:9px;color:var(--text3);cursor:pointer;text-align:right}
.hint-copy:hover{color:var(--text2)}

/* All cards table inside group */
.all-cards{padding:0}
.all-cards table{width:100%;border-collapse:collapse}
.all-cards th{font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;
  padding:8px 14px;text-align:left;border-bottom:1px solid var(--border);background:var(--surface)}
.all-cards td{padding:7px 14px;border-bottom:1px solid rgba(63,63,70,.3);font-size:12px}
.all-cards tr:hover{background:rgba(59,130,246,.03)}
.t-side{font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px}
.t-yes{background:var(--green-dim);color:var(--green)}
.t-no{background:var(--red-dim);color:var(--red)}
.t-w{background:var(--surface2);color:var(--text3)}
.t-phrase{font-weight:600;font-size:13px}
.t-num{font-family:'JetBrains Mono',monospace;font-size:12px}
.t-ev{font-weight:700}.t-ev.pos{color:var(--green)}.t-ev.neg{color:var(--text3)}
.t-poly{color:var(--purple);font-weight:600}
.t-hint{font-size:11px}
.sz-badge{display:inline-block;margin-left:5px;padding:1px 5px;border-radius:3px;font-size:10px;font-weight:600;background:rgba(250,204,21,.15);color:#ca8a04;border:1px solid rgba(250,204,21,.3);vertical-align:middle}

/* Discrepancy badge */
.poly-flag{display:inline-flex;align-items:center;gap:4px;font-size:10px;font-weight:600;
  padding:2px 7px;border-radius:4px;background:var(--purple-dim);color:var(--purple)}

/* Tags */
.tag{font-size:9px;font-weight:600;padding:2px 6px;border-radius:3px;display:inline-block}
.tag-news{background:var(--amber-dim);color:var(--amber)}
.tag-poly{background:var(--purple-dim);color:var(--purple)}
.tag-base{background:var(--green-dim);color:var(--green)}
.tag-confirmed{background:rgba(34,197,94,.15);color:var(--green);font-weight:700;animation:pulse 2s ease-in-out infinite}
.tag-anchor{background:rgba(251,191,36,.15);color:var(--amber);font-weight:600}
.tag-on-topic{background:rgba(34,197,94,.12);color:var(--green);font-weight:600}
.tag-off-topic{background:rgba(239,68,68,.12);color:var(--red);font-weight:600}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.7}}
.tag-hist{background:var(--surface2);color:var(--text2);font-weight:500}
.hist-rate{font-size:10px;color:var(--text2);font-family:'JetBrains Mono',monospace}
.hist-rate .hr-pct{font-weight:700}
.hist-rate.hr-high .hr-pct{color:var(--green)}
.hist-rate.hr-low .hr-pct{color:var(--red)}
.tag-thin{background:var(--red-dim);color:var(--red)}
.tag-spread{background:var(--amber-dim);color:var(--amber)}

/* Poly vs Kalshi tab */
.disc-table{border:1px solid var(--border);border-radius:var(--r);overflow:hidden;background:var(--surface)}
.disc-table table{width:100%;border-collapse:collapse}
.disc-table th{font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;
  padding:10px 14px;text-align:left;border-bottom:1px solid var(--border);background:var(--surface)}
.disc-table td{padding:9px 14px;border-bottom:1px solid rgba(63,63,70,.3);font-size:13px}
.disc-table tr:hover{background:rgba(59,130,246,.03)}
.d-under{color:var(--green);font-weight:600}
.d-over{color:var(--red);font-weight:600}
.d-diff{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:14px}
.d-big{color:var(--purple)}

/* Health / System */
.health-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:8px}
.h-item{background:var(--surface);border:1px solid var(--border);border-radius:var(--rs);
  padding:12px 16px;display:flex;justify-content:space-between;align-items:center}
.h-item .hl{font-size:12px;color:var(--text2)}
.h-item .hv{font-size:15px;font-weight:700;font-family:'JetBrains Mono',monospace}

/* Coverage tab */
.cov-table{border:1px solid var(--border);border-radius:var(--r);overflow:hidden;background:var(--surface)}
.cov-table table{width:100%;border-collapse:collapse}
.cov-table th{font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;
  padding:10px 14px;text-align:left;border-bottom:1px solid var(--border);background:var(--surface)}
.cov-table td{padding:9px 14px;border-bottom:1px solid rgba(63,63,70,.3);font-size:12px;vertical-align:top}
.cov-table tr:hover{background:rgba(59,130,246,.03)}
.cov-yes{color:var(--green);font-weight:600}
.cov-no{color:var(--amber);font-weight:600}
.cov-reason{display:inline-flex;align-items:center;font-size:10px;font-weight:600;padding:2px 7px;border-radius:10px;margin-right:5px}
.cov-miss{background:var(--red-dim);color:var(--red)}
.cov-cache{background:var(--amber-dim);color:var(--amber)}
.cov-shape{background:var(--blue-dim);color:var(--blue)}
.cov-poly{background:var(--purple-dim);color:var(--purple)}

/* ── Model vs Market text indicator (replaces gauge) ── */
.div-inline{display:inline-flex;align-items:center;gap:4px;font-size:10px;color:var(--text3);margin-top:2px}
.div-inline .di-model{font-family:'JetBrains Mono',monospace;color:var(--amber);font-weight:600}
.div-inline .di-sep{opacity:.4}
.div-inline .di-mkt{font-family:'JetBrains Mono',monospace;font-weight:600}
.div-inline .di-dir{font-weight:600}

/* ── Gate badges ── */
.gate-badge{font-size:9px;font-weight:700;padding:2px 7px;border-radius:4px;display:inline-flex;align-items:center;gap:3px}
.gate-no-floor{background:rgba(239,68,68,.12);color:#f87171;border:1px solid rgba(239,68,68,.2)}
.gate-bearish{background:rgba(245,158,11,.12);color:#fbbf24;border:1px solid rgba(245,158,11,.2)}
.gate-veto-no{background:rgba(168,85,247,.12);color:#c084fc;border:1px solid rgba(168,85,247,.2)}
.gate-veto-yes{background:rgba(168,85,247,.12);color:#c084fc;border:1px solid rgba(168,85,247,.2)}
.gate-poly-veto{background:rgba(59,130,246,.12);color:#93c5fd;border:1px solid rgba(59,130,246,.2)}

/* ── Intelligence tab v2 ── */
.int-summary{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px}
@media(max-width:900px){.int-summary{grid-template-columns:repeat(2,1fr)}}
.int-kpi{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:14px 16px;text-align:center}
.int-kpi .kv{font-size:28px;font-weight:800;letter-spacing:-.03em;line-height:1.1}
.int-kpi .kv.kv-green{color:var(--green)}.int-kpi .kv.kv-red{color:var(--red)}.int-kpi .kv.kv-amber{color:var(--amber)}.int-kpi .kv.kv-purple{color:#c084fc}
.int-kpi .kl{font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;margin-top:4px}
.int-pipeline{display:flex;flex-wrap:wrap;gap:8px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:10px 14px;margin-bottom:16px;align-items:center}
.int-pip-item{display:flex;align-items:center;gap:5px;font-size:11px;color:var(--text2)}
.int-pip-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.int-pip-dot.ok{background:var(--green)}.int-pip-dot.warn{background:var(--amber)}.int-pip-dot.err{background:var(--red)}
.int-topics{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px;margin-bottom:20px}
.int-topic{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:14px 16px;transition:border-color .15s}
.int-topic:hover{border-color:var(--border2)}
.int-topic-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.int-topic-name{font-size:14px;font-weight:700}
.int-topic-prob{font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px}
.int-topic-prob.high{background:var(--green-dim);color:var(--green)}
.int-topic-prob.med{background:var(--amber-dim);color:var(--amber)}
.int-topic-prob.low{background:var(--surface2);color:var(--text3)}
.int-topic-ev{font-size:12px;color:var(--text2);line-height:1.45;margin-bottom:8px}
.int-topic-phrases{display:flex;flex-wrap:wrap;gap:4px}
.int-topic-pill{font-size:9px;font-weight:600;padding:2px 7px;border-radius:3px;background:rgba(168,85,247,.12);color:#c084fc;border:1px solid rgba(168,85,247,.2)}
.int-cat{margin-bottom:12px;border:1px solid var(--border);border-radius:var(--r);overflow:hidden}
.int-cat-head{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;cursor:pointer;transition:background .1s}
.int-cat-head:hover{background:var(--surface2)}
.int-cat-head.has-buy{background:rgba(34,197,94,.05);border-bottom:1px solid rgba(34,197,94,.15)}
.int-cat-left{display:flex;align-items:center;gap:8px}
.int-cat-icon{font-size:18px}
.int-cat-name{font-size:13px;font-weight:700}
.int-cat-sub{font-size:11px;color:var(--text3)}
.int-cat-badges{display:flex;align-items:center;gap:6px}
.int-buy-badge{font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px;background:rgba(34,197,94,.12);color:var(--green)}
.int-tbl{width:100%;border-collapse:collapse}
.int-tbl th{font-size:10px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.04em;padding:7px 12px;text-align:left;background:var(--surface2);border-bottom:1px solid var(--border)}
.int-tbl th.r{text-align:right}.int-tbl th.c{text-align:center}
.int-tbl td{padding:8px 12px;border-bottom:1px solid rgba(63,63,70,.2);font-size:12px}
.int-tbl td.r{text-align:right}.int-tbl td.c{text-align:center}
.int-tbl td.mono{font-family:'JetBrains Mono','SF Mono',monospace;font-size:11px}
.int-tbl tr.buy-row{background:rgba(34,197,94,.03)}
.int-tbl tr.buy-row:hover{background:rgba(34,197,94,.07)}
.int-tbl tr:not(.buy-row):hover{background:rgba(59,130,246,.03)}
.int-tbl tr.detail-row{background:var(--surface)}
.int-tbl tr.detail-row td{padding:10px 14px}
.int-ev-pos{color:var(--green);font-weight:700}.int-ev-neg{color:var(--text3)}
.int-llm-up{color:var(--green);font-weight:700}.int-llm-dn{color:var(--red);font-weight:700}.int-llm-nil{color:var(--text3)}
.int-conf-h{color:var(--green);font-weight:600}.int-conf-m{color:var(--amber);font-weight:600}.int-conf-l{color:var(--red);font-weight:600}
.int-detail-topic{font-size:11px;font-weight:600;color:#c084fc;margin-bottom:3px}
.int-detail-reason{font-size:12px;color:var(--text2);line-height:1.45;margin-bottom:3px}
.int-detail-evidence{font-size:11px;color:var(--text3);font-style:italic}
.int-direct-badge{display:inline-block;margin-top:4px;font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px;background:rgba(34,197,94,.15);color:var(--green)}
.int-section-head{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);margin:24px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.ai-model-badge{display:inline-flex;align-items:center;gap:5px;font-size:10px;font-weight:600;padding:3px 9px;border-radius:20px;background:rgba(168,85,247,.1);color:#c084fc;border:1px solid rgba(168,85,247,.2)}
.llm-boost-pill{font-size:11px;font-weight:800;padding:1px 7px;border-radius:3px}
.llm-up .llm-boost-pill{background:var(--green-mid);color:var(--green)}
.llm-down .llm-boost-pill{background:var(--red-mid);color:var(--red)}
.llm-topic{font-size:9px;background:rgba(168,85,247,.15);color:#c084fc;padding:1px 6px;border-radius:3px;font-weight:600}

/* Empty */
.empty{text-align:center;padding:50px 20px;color:var(--text3)}
.empty h3{font-size:15px;font-weight:600;color:var(--text2);margin-bottom:6px}
.empty p{font-size:12px}
.empty code{background:var(--surface2);padding:2px 8px;border-radius:4px;font-size:12px}

@media(max-width:700px){
  .sh{padding:12px 10px 40px}
  .stats{grid-template-columns:repeat(3,1fr)}
  header{flex-direction:column;align-items:flex-start}
  .bet-card{flex-wrap:wrap;gap:12px}
  .bc-right{flex-direction:row;align-items:flex-end;justify-content:space-between;width:100%;min-width:unset}
  .bc-ev-wrap{display:flex;align-items:baseline;gap:8px}
  .bc-ev-lbl{margin-top:0}
}

/* ── Scripts tab ── */
.scripts-page{display:flex;flex-direction:column;gap:28px}
.sg-head{display:flex;align-items:center;gap:10px;margin-bottom:12px}
.sg-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--text3);white-space:nowrap}
.sg-line{flex:1;height:1px;background:var(--border)}
.sg-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:10px}
.sc-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:14px 16px;
  display:flex;flex-direction:column;gap:10px;transition:border-color .15s,box-shadow .15s}
.sc-card:hover{border-color:rgba(59,130,246,.25)}
.sc-card.sc-running{border-color:var(--blue);box-shadow:0 0 0 1px rgba(59,130,246,.15)}
.sc-card.sc-done{border-color:rgba(34,197,94,.35)}
.sc-card.sc-error{border-color:rgba(239,68,68,.35)}
.sc-top{display:flex;align-items:flex-start;gap:10px}
.sc-info{flex:1;min-width:0}
.sc-name{font-size:13px;font-weight:700;letter-spacing:-.01em;margin-bottom:4px}
.sc-desc{font-size:11px;color:var(--text3);line-height:1.45}
.sc-meta{display:flex;align-items:center;gap:6px;margin-top:5px}
.sc-tag{font-size:9px;font-weight:700;padding:2px 8px;border-radius:3px;text-transform:uppercase;letter-spacing:.04em}
.sc-tag.t-preevent{background:rgba(250,204,21,.15);color:#fde047;border:1px solid rgba(250,204,21,.35)}
.sc-tag.t-llm{background:rgba(168,85,247,.12);color:#c084fc;border:1px solid rgba(168,85,247,.25)}
.sc-tag.t-data{background:rgba(59,130,246,.12);color:#93c5fd;border:1px solid rgba(59,130,246,.25)}
.sc-tag.t-backtest{background:rgba(245,158,11,.12);color:#fbbf24;border:1px solid rgba(245,158,11,.25)}
.sc-tag.t-calib{background:rgba(34,197,94,.12);color:#4ade80;border:1px solid rgba(34,197,94,.25)}
.sc-tag.t-health{background:rgba(20,184,166,.12);color:#2dd4bf;border:1px solid rgba(20,184,166,.25)}
.sc-tag.t-corpus{background:rgba(249,115,22,.12);color:#fb923c;border:1px solid rgba(249,115,22,.25)}
.sc-tag.t-danger{background:rgba(239,68,68,.12);color:#f87171;border:1px solid rgba(239,68,68,.3)}
.sc-tag.t-engine{background:rgba(34,197,94,.12);color:#4ade80;border:1px solid rgba(34,197,94,.3)}
.sc-tag.t-nba{background:rgba(249,115,22,.12);color:#f97316;border:1px solid rgba(249,115,22,.3)}
.sc-file{font-size:9px;color:var(--text3);font-family:'SF Mono',Menlo,Monaco,monospace;opacity:.6}
.sc-btn{flex-shrink:0;padding:7px 16px;font-size:12px;font-weight:600;border-radius:6px;
  border:1px solid var(--border);background:var(--surface2);color:var(--text);cursor:pointer;
  transition:all .12s;white-space:nowrap;align-self:flex-start}
.sc-btn:hover:not([disabled]){border-color:var(--blue);background:rgba(59,130,246,.1);color:var(--blue)}
.sc-btn[disabled]{opacity:.45;cursor:not-allowed}
.sc-btn.btn-running{border-color:var(--blue);color:var(--blue);background:rgba(59,130,246,.08)}
.sc-btn.btn-done{border-color:rgba(34,197,94,.45);color:var(--green);background:rgba(34,197,94,.06)}
.sc-btn.btn-error{border-color:rgba(239,68,68,.45);color:var(--red);background:rgba(239,68,68,.06)}
.sc-out{border-top:1px solid var(--border);padding-top:10px;display:none}
.sc-out.visible{display:block}
.sc-status-bar{display:flex;align-items:center;gap:7px;margin-bottom:7px;font-size:11px;color:var(--text3)}
.sc-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;transition:background .2s}
.sc-dot.d-running{background:var(--blue);animation:sc-pulse 1.1s ease-in-out infinite}
.sc-dot.d-done{background:var(--green)}
.sc-dot.d-error{background:var(--red)}
@keyframes sc-pulse{0%,100%{opacity:1}50%{opacity:.25}}
.sc-elapsed{margin-left:auto;font-size:10px;color:var(--text3);font-family:'SF Mono',Menlo,Monaco,monospace}
.sc-term{background:#0d0d0f;border:1px solid rgba(255,255,255,.07);border-radius:6px;
  padding:10px 12px;font-family:'SF Mono',Menlo,Monaco,monospace;font-size:10.5px;
  line-height:1.55;color:#d4d4d8;max-height:220px;overflow-y:auto;
  white-space:pre-wrap;word-break:break-all;min-height:40px}
.sc-term .t-err{color:#f87171}.sc-term .t-warn{color:#fbbf24}.sc-term .t-ok{color:#4ade80}
/* toast */
.sc-toast{position:fixed;bottom:24px;left:50%;transform:translateX(-50%);background:#1c1c1e;border:1px solid var(--border);
  border-radius:8px;padding:10px 18px;font-size:12px;font-weight:500;color:var(--text);
  box-shadow:0 4px 24px rgba(0,0,0,.5);z-index:9999;pointer-events:none;
  animation:toast-in .2s ease;transition:opacity .3s}
.sc-toast.warn{border-color:rgba(245,158,11,.45);color:#fbbf24}
.sc-toast.err{border-color:rgba(239,68,68,.45);color:#f87171}
@keyframes toast-in{from{opacity:0;transform:translateX(-50%) translateY(8px)}to{opacity:1;transform:translateX(-50%) translateY(0)}}
/* danger group — only cards with t-danger tag get red treatment */
.sg-danger .sc-card:has(.t-danger){border-color:rgba(239,68,68,.2);background:rgba(239,68,68,.03)}
.sg-danger .sc-card:has(.t-danger):hover{border-color:rgba(239,68,68,.4)}
.sg-danger .sc-card:has(.t-danger) .sc-btn{border-color:rgba(239,68,68,.35);color:#f87171}
.sg-danger .sc-card:has(.t-danger) .sc-btn:hover:not([disabled]){background:rgba(239,68,68,.1);border-color:rgba(239,68,68,.6);color:#f87171}
.sg-danger .sg-label{color:var(--text3)}
.sg-preevent .sc-card:has(.t-preevent){border-color:rgba(250,204,21,.25);background:rgba(250,204,21,.04)}
.sg-preevent .sc-card:has(.t-preevent):hover{border-color:rgba(250,204,21,.45)}
.sg-preevent .sc-card:has(.t-preevent) .sc-btn{border-color:rgba(250,204,21,.35);color:#fde047}
.sg-preevent .sc-card:has(.t-preevent) .sc-btn:hover:not([disabled]){background:rgba(250,204,21,.1);border-color:rgba(250,204,21,.6);color:#fde047}
.sg-preevent .sg-label{color:#fde047}
.sg-sports .sc-card:has(.t-nba){border-color:rgba(249,115,22,.25);background:rgba(249,115,22,.04)}
.sg-sports .sc-card:has(.t-nba):hover{border-color:rgba(249,115,22,.45)}
.sg-sports .sc-card:has(.t-nba) .sc-btn{border-color:rgba(249,115,22,.35);color:#f97316}
.sg-sports .sc-card:has(.t-nba) .sc-btn:hover:not([disabled]){background:rgba(249,115,22,.1);border-color:rgba(249,115,22,.6);color:#f97316}
.sg-sports .sg-label{color:#f97316}

/* ── Outcomes tab ── */
.oc-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px;margin-bottom:20px}
.oc-block{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:16px;display:flex;flex-direction:column;gap:10px}
.oc-side-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text3)}
.oc-stat{display:flex;flex-direction:column;gap:1px}
.oc-val{font-size:22px;font-weight:700;letter-spacing:-.02em}
.oc-val.g{color:var(--green)}.oc-val.a{color:var(--amber)}.oc-val.r{color:var(--red)}
.oc-lbl{font-size:10px;color:var(--text3)}
.oc-section-head{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);margin:20px 0 8px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.badge-g{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;background:var(--green-dim);color:var(--green)}
.badge-a{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;background:var(--amber-dim);color:var(--amber)}
.badge-r{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:700;background:var(--red-dim);color:var(--red)}
.side-pill{font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px;text-transform:uppercase;letter-spacing:.04em}
.side-pill.by{background:var(--green-dim);color:var(--green)}.side-pill.bn{background:var(--red-dim);color:var(--red)}
.t-red{color:var(--red)!important}

/* ── Signals tab ── */
.sig-health-bar{display:flex;flex-wrap:wrap;gap:8px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:14px 16px;margin-bottom:20px;align-items:center}
.sig-health-item{display:flex;align-items:center;gap:6px}
.sig-health-label{font-size:11px;font-weight:600;color:var(--text2)}
.sig-badge{font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px}
.sig-badge-ok{background:rgba(34,197,94,.12);color:#4ade80;border:1px solid rgba(34,197,94,.25)}
.sig-badge-warn{background:rgba(245,158,11,.12);color:#fbbf24;border:1px solid rgba(245,158,11,.25)}
.sig-badge-err{background:rgba(239,68,68,.12);color:#f87171;border:1px solid rgba(239,68,68,.25)}
.ts-feed{display:flex;flex-direction:column;gap:10px;margin-bottom:20px}
.ts-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:14px 16px;transition:border-color .12s}
.ts-card:hover{border-color:var(--border2)}
.ts-card-top{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.ts-age{font-size:11px;font-weight:600;color:var(--text3)}
.ts-boost{font-size:10px;font-weight:700;padding:2px 8px;border-radius:4px}
.ts-boost.boost-high{background:rgba(34,197,94,.12);color:#4ade80;border:1px solid rgba(34,197,94,.25)}
.ts-boost.boost-mid{background:rgba(245,158,11,.12);color:#fbbf24;border:1px solid rgba(245,158,11,.25)}
.ts-boost.boost-low{background:rgba(100,116,139,.12);color:var(--text3);border:1px solid rgba(100,116,139,.2)}
.ts-url{font-size:10px;color:var(--blue);text-decoration:none;margin-left:auto}
.ts-url:hover{text-decoration:underline}
.ts-content{font-size:13px;color:var(--text);line-height:1.5;word-break:break-word}
.ts-id{font-size:9px;color:var(--text3);font-family:'SF Mono',Menlo,Monaco,monospace;margin-top:6px;opacity:.5}

/* ── System tab v2 ── */
.sys-page{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:800px){.sys-page{grid-template-columns:1fr}}
.sys-section{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:16px 18px}
.sys-section.sys-full{grid-column:1/-1}
.sys-section-head{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);margin-bottom:10px;display:flex;align-items:center;gap:8px}
.sys-fix-hint{font-size:11px;color:var(--text3);margin-bottom:10px;padding:5px 9px;background:var(--surface2);border-radius:5px;border-left:3px solid var(--border2);line-height:1.5}
.sys-list{display:flex;flex-direction:column}
.h-item-v2{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border1);min-height:28px}
.h-item-v2:last-child{border-bottom:none}
.hl{font-size:12px;color:var(--text3);flex:1;min-width:0}
.hv{font-size:12px;font-weight:600;color:var(--text);font-family:'JetBrains Mono',monospace;text-align:right;white-space:nowrap}
.hd{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.hd-ok{background:var(--green)}.hd-warn{background:var(--amber)}.hd-err{background:var(--red)}.hd-info{background:var(--blue)}
.sys-alert-row{display:flex;align-items:flex-start;gap:6px;padding:5px 0;border-bottom:1px solid var(--border1);font-size:11px}
.sys-alert-row:last-child{border-bottom:none}
.sys-badge{font-size:10px;font-weight:700;padding:1px 6px;border-radius:10px;white-space:nowrap;flex-shrink:0}
.sys-badge-high{background:rgba(239,68,68,.18);color:#f87171}
.sys-badge-med{background:rgba(251,191,36,.18);color:#fbbf24}
.sys-badge-low{background:rgba(99,102,241,.18);color:#818cf8}

/* ── Analysis sub-tabs ── */
.ana-tabs{display:flex;gap:2px;margin-bottom:16px;border-bottom:1px solid var(--border)}
.ana-tab{padding:8px 14px;font-size:12px;font-weight:500;color:var(--text3);cursor:pointer;border-bottom:2px solid transparent;transition:all .12s;user-select:none}
.ana-tab:hover{color:var(--text2)}
.ana-tab.on{color:var(--text);border-bottom-color:var(--blue)}

/* tier-a handled inside .bet-card rules above */

/* ── LLM confidence + direct-evidence badges ── */
.llm-conf-badge{font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px;display:inline-block}
.llm-conf-high{background:rgba(34,197,94,.12);color:var(--green)}
.llm-conf-medium{background:rgba(245,158,11,.12);color:var(--amber)}
.llm-conf-low{background:rgba(113,113,122,.12);color:var(--text3)}
.llm-ev-badge{font-size:9px;font-weight:600;padding:1px 5px;border-radius:3px;background:rgba(59,130,246,.1);color:var(--blue);display:inline-block}

/* ── WATCH section toggle inside market group ── */
.watch-toggle{padding:10px 18px;font-size:12px;color:var(--text3);cursor:pointer;border-top:1px solid rgba(63,63,70,.3);user-select:none;transition:color .1s}
.watch-toggle:hover{color:var(--text2)}
.watch-cards{background:var(--surface)}
</style>
</head>
<body>
<div class="sh">

<header>
  <div class="logo">
    <h1>Kalshi Edge</h1>
    <span class="live">Live</span>
  </div>
  <div class="ctrls">
    <select id="auto-r" onchange="setAR()">
      <option value="0">Manual</option>
      <option value="5" selected>5s</option>
      <option value="10">10s</option>
      <option value="30">30s</option>
    </select>
    <button onclick="doRefresh()">Refresh</button>
    <span class="ts" id="lr">...</span>
  </div>
</header>

<div class="stats" id="stats"></div>

<div class="tabs" id="tabs">
  <div class="tab on" data-t="markets" onclick="sw('markets')">Markets</div>
  <div class="tab" data-t="sports" onclick="sw('sports')">Sports</div>
  <div class="tab" data-t="intelligence" onclick="sw('intelligence')" id="tab-intel">Intelligence</div>
  <div class="tab" data-t="performance" onclick="sw('performance')">Performance</div>
  <div class="tab" data-t="analysis" onclick="sw('analysis')">Analysis</div>
  <div class="tab" data-t="system" onclick="sw('system')">System</div>
  <div class="tab" data-t="scripts" onclick="sw('scripts')">Scripts</div>
</div>

<div id="content"></div>

</div>

<script>
let CT='markets',RT=null,D={},openGroups=new Set();
const SPEAKER_ORDER=['trump','leavitt','mamdani','fed','ncaab','nba','mlb','mma'];
const SPEAKER_LABELS={
  trump:'Donald Trump',leavitt:'Karoline Leavitt',mamdani:'Zohran Mamdani',
  fed:'Powell / Fed',ncaab:'NCAAB Basketball',nba:'NBA Basketball',
  mlb:'MLB Baseball',mma:'MMA / UFC',
};
function togglePerfCard(id){
  const el=document.getElementById(id);if(!el)return;
  const open=el.style.display!=='none';
  el.style.display=open?'none':'block';
  const row=el.closest('.pi-spk-row')||el.closest('.perf-spk-card');
  if(row){row.classList.toggle('pi-open',!open);row.classList.toggle('perf-open',!open);}
}

function perfShortLabel(spk,lbl){
  if(spk==='ncaab')return 'NCAAB';
  if(spk==='nba')return 'NBA';
  if(spk==='mlb')return 'MLB';
  if(spk==='mma')return 'MMA';
  if(spk==='fed')return 'Fed';
  const parts=lbl.split(' ');
  return parts.length>1?parts[parts.length-1]:lbl;
}
function perfScrollToSpk(spk){
  const el=document.getElementById('perf-spk-'+spk);
  if(el)el.scrollIntoView({behavior:'smooth',block:'start'});
}

function sw(t){
  CT=t;
  document.querySelectorAll('.tab').forEach(el=>el.classList.toggle('on',el.dataset.t===t));
  render();
}

function setAR(){
  if(RT)clearInterval(RT);
  const s=+document.getElementById('auto-r').value;
  if(s>0)RT=setInterval(doRefresh,s*1000);
}

async function doRefresh(){
  document.getElementById('lr').textContent='...';
  try{
    const r=await fetch('/api/data?t='+Date.now());
    D=await r.json();
    showApiErrors(D._api_errors||[]);
    renderStats();
    render();
    document.getElementById('lr').textContent=new Date().toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
  }catch(e){
    document.getElementById('lr').textContent='err';
    showApiErrors(['Dashboard API unreachable: '+e.message]);
  }
}

function showApiErrors(errs){
  let el=document.getElementById('api-error-banner');
  if(!el){
    el=document.createElement('div');
    el.id='api-error-banner';
    el.style.cssText='position:fixed;top:0;left:0;right:0;z-index:9999;padding:10px 16px;font-size:13px;display:none;align-items:center;gap:8px;';
    document.body.prepend(el);
  }
  if(!errs.length){el.style.display='none';return;}
  el.style.display='flex';
  el.style.background='#2d1518';el.style.borderBottom='2px solid #f44';el.style.color='#faa';
  el.innerHTML='<span style="flex:1"><b>System Issues ('+errs.length+'):</b> '+errs.map(e=>'<code style="background:#1a1a2e;padding:2px 6px;border-radius:3px;font-size:12px">'+e.replace(/</g,'&lt;')+'</code>').join(' ')+'</span>'
    +'<button onclick="runRepair()" style="background:#f44;color:#fff;border:none;padding:6px 16px;border-radius:4px;cursor:pointer;font-weight:600;white-space:nowrap">Repair System</button>'
    +'<button onclick="this.parentElement.style.display=\'none\'" style="background:transparent;color:#faa;border:1px solid #faa;padding:6px 10px;border-radius:4px;cursor:pointer">Dismiss</button>';
}

async function runRepair(){
  if(!confirm('This will stop the engine, repair the database, and restart.\\nContinue?'))return;
  try{
    const r=await fetch('/api/run-script',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({script:'scripts/repair_system.py',id:'repair_system'})});
    const d=await r.json();
    if(d.job_id){
      alert('Repair started! Switch to the Scripts tab to see progress.\\nThe dashboard will auto-refresh when done.');
      const tabs=document.querySelectorAll('[data-tab]');
      tabs.forEach(t=>{if(t.dataset.tab==='scripts')t.click();});
    }else{
      alert('Failed to start repair: '+(d.error||'unknown error'));
    }
  }catch(e){alert('Failed: '+e.message);}
}

function renderStats(){
  const s=D.status;if(!s)return;
  const age=s.snapshot_age_sec;
  const ac=age<60?'g':age<300?'a':'r';
  const as=age<60?age+'s':age<3600?Math.round(age/60)+'m':Math.round(age/3600)+'h';
  // Use effective boosts from signals.yaml (confidence-adjusted), not stated boosts from llm_analysis.json
  const sigYaml=((D.signals_intel)||{}).signals_yaml||{};
  const llmCount=sigYaml.boosted||0;
  const llmAge=(D.llm_analysis||{}).analyzed_at?timeSince((D.llm_analysis||{}).analyzed_at):'—';
  const allCards=D.all_cards||[];
  const blockedMktIds=new Set(allCards.filter(c=>c.side==='WATCH'&&!c.gate_pass).map(c=>c.market_id));
  const gateBlocks=blockedMktIds.size;
  const totalBuy=(s.buy_yes_count||0)+(s.buy_no_count||0);
  // P&L + Win Rate from resolved outcomes
  const oc=D.outcomes||{};
  const pnl=oc.total_pnl!=null?oc.total_pnl:null;
  const wr=oc.win_rate!=null?oc.win_rate:null;
  const pnlStr=pnl!=null?(pnl>=0?'+':'')+`$${Math.abs(pnl).toFixed(2)}`:'—';
  const wrStr=wr!=null?Math.round(wr*100)+'%':'—';
  const pnlCls=pnl==null?'b':pnl>=0?'g':'r';
  const wrCls=wr==null?'b':wr>=0.55?'g':wr>=0.40?'a':'r';
  document.getElementById('stats').innerHTML=`
    <div class="st"><div class="sl">Buy Yes</div><div class="sv g">${s.buy_yes_count}</div><div class="ss">active signals</div></div>
    <div class="st"><div class="sl">Buy No</div><div class="sv r">${s.buy_no_count}</div><div class="ss">active signals</div></div>
    <div class="st"><div class="sl">Markets</div><div class="sv b">${s.market_count}</div><div class="ss">tracked</div></div>
    <div class="st"><div class="sl">AI Boosts</div><div class="sv p">${llmCount}</div><div class="ss">effective · ${llmAge} ago</div></div>
    <div class="st"><div class="sl">Filtered Out</div><div class="sv a">${gateBlocks}</div><div class="ss">gate blocks</div></div>
    <div class="st"><div class="sl">Net P&amp;L</div><div class="sv ${pnlCls}">${pnlStr}</div><div class="ss">${oc.total||0} resolved bets</div></div>
    <div class="st"><div class="sl">Win Rate</div><div class="sv ${wrCls}">${wrStr}</div><div class="ss">${oc.wins||0} wins</div></div>
    <div class="st"><div class="sl">Data Age</div><div class="sv ${ac}">${as}</div><div class="ss">${s.phrase_hit_count||0} phrase hits</div></div>
  `;
  document.title=totalBuy>0?`(${totalBuy}) Kalshi Edge`:'Kalshi Edge';
  // Badge on Intelligence tab when active boosts exist
  const intelTab=document.getElementById('tab-intel');
  if(intelTab){intelTab.innerHTML=llmCount>0?`Intelligence <span style="display:inline-flex;align-items:center;justify-content:center;background:var(--purple);color:#fff;border-radius:50%;width:16px;height:16px;font-size:9px;font-weight:700;margin-left:4px">${llmCount}</span>`:'Intelligence';}
  // Badge on Markets tab when BUY cards exist
  const mktsTab=document.querySelector('.tab[data-t="markets"]');
  if(mktsTab){mktsTab.innerHTML=totalBuy>0?`Markets <span style="display:inline-flex;align-items:center;justify-content:center;background:var(--green);color:#fff;border-radius:50%;width:16px;height:16px;font-size:9px;font-weight:700;margin-left:4px">${totalBuy}</span>`:'Markets';}
}

function timeSince(iso){
  try{
    const diff=(Date.now()-new Date(iso).getTime())/1000;
    if(diff<60)return Math.round(diff)+'s';
    if(diff<3600)return Math.round(diff/60)+'m';
    return Math.round(diff/3600)+'h';
  }catch(e){return'?'}
}

function render(){
  const el=document.getElementById('content');
  if(CT==='markets')el.innerHTML=renderMarkets();
  else if(CT==='sports')el.innerHTML=renderSports();
  else if(CT==='intelligence')el.innerHTML=renderIntelligence();
  else if(CT==='performance')el.innerHTML=renderPerformance();
  else if(CT==='analysis'){el.innerHTML=renderAnalysis();bindAnalysis();}
  else if(CT==='system')el.innerHTML=renderSystem();
  else if(CT==='scripts'){el.innerHTML=renderScripts();bindScripts();}
}

/* ─── AI Intelligence tab ─── */
function renderLLM(){
  const la=D.llm_analysis||{};
  if(!la.topics&&!la.assessments){
    return emp('No AI Analysis Yet','Run <b>Analyze Global Signals</b> from the Scripts tab');
  }
  const topics=(la.topics||[]);
  const boosts=(la.assessments||[]).filter(a=>a.boost>1.0).sort((a,b)=>b.boost-a.boost);
  const suppresses=(la.assessments||[]).filter(a=>a.boost<1.0).sort((a,b)=>a.boost-b.boost);
  const analyzedAt=la.analyzed_at?new Date(la.analyzed_at).toLocaleString():'Never';
  // Use the live configured model from env (_signal_model), not the stale cached model name
  const configuredModel=la._signal_model||la.model||'—';
  const cachedModel=la.model||'—';
  const modelStale=cachedModel!==configuredModel&&cachedModel!=='—'&&configuredModel!=='—';
  const phraseCount=la.phrases_evaluated||0;
  // Staleness warning: if analyzed_at > 60 min ago
  const ageMs=la.analyzed_at?(Date.now()-new Date(la.analyzed_at).getTime()):Infinity;
  const isStale=ageMs>60*60*1000;
  const ageLabel=la.analyzed_at?timeSince(la.analyzed_at)+' ago':'never run';

  let h=`<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:8px">
    <div>
      <h2 style="font-size:18px;font-weight:800;letter-spacing:-.02em;margin-bottom:2px">AI Signal Intelligence</h2>
      <p style="font-size:12px;color:var(--text3)">${configuredModel} analysis of ${phraseCount} live market phrases against today's political signals</p>
    </div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <span class="ai-model-badge">${configuredModel}</span>
      ${modelStale?`<span style="font-size:10px;font-weight:600;padding:2px 8px;border-radius:3px;background:rgba(245,158,11,.12);color:#fbbf24;border:1px solid rgba(245,158,11,.25)">cache: ${cachedModel}</span>`:''}
      <span style="font-size:11px;color:${isStale?'var(--amber)':'var(--text3)'}">Updated ${ageLabel}${isStale?' — run Analyze Global Signals':''}</span>
    </div>
  </div>`;

  h+=`<div class="ai-grid">`;

  // Topics panel
  h+=`<div class="ai-section">
    <div class="ai-section-head">
      <h3>Today's Key Topics</h3>
      <span class="ai-badge">${topics.length} identified</span>
    </div>
    <div class="topic-list">`;
  if(!topics.length){h+='<p style="color:var(--text3);font-size:12px;padding:8px">No topics extracted yet.</p>';}
  for(const t of topics){
    const pCls=t.probability==='high'?'prob-high':t.probability==='medium'?'prob-medium':'prob-low';
    const pc=t.probability==='high'?'high':t.probability==='medium'?'medium':'low';
    const phrases=(t.likely_phrases||[]).map(p=>`<span class="topic-phrase-pill">${p}</span>`).join('');
    h+=`<div class="topic-card ${pCls}">
      <div class="topic-name">
        ${t.topic||''}
        <span class="topic-prob ${pc}">${(t.probability||'').toUpperCase()}</span>
      </div>
      <div class="topic-evidence">${t.evidence||''}</div>
      ${t.reasoning?`<div class="topic-reasoning">${t.reasoning}</div>`:''}
      ${phrases?`<div class="topic-phrases">${phrases}</div>`:''}
    </div>`;
  }
  h+=`</div></div>`;

  // Boosts panel
  h+=`<div class="ai-section">
    <div class="ai-section-head">
      <h3>Phrase Probability Adjustments</h3>
      <span class="ai-badge">${boosts.length} boosts · ${suppresses.length} suppressed</span>
    </div>
    <div class="boost-list">`;
  if(!boosts.length&&!suppresses.length){h+='<p style="color:var(--text3);font-size:12px;padding:8px">No adjustments applied — all phrases neutral.</p>';}
  for(const a of [...boosts,...suppresses]){
    const isUp=a.boost>=1.0;
    const icon=isUp?'↑':'↓';
    const pct=Math.abs(Math.round((a.boost-1.0)*100));
    const cls=isUp?'up':'down';
    h+=`<div class="boost-row">
      <div class="boost-dir" style="color:${isUp?'var(--green)':'var(--red)'}">${icon}</div>
      <div class="boost-phrase">
        <div class="boost-name">"${a.phrase}"</div>
        ${a.topic?`<div class="boost-topic">${a.topic}</div>`:''}
        ${a.reasoning?`<div class="boost-reasoning">${a.reasoning}</div>`:''}
        ${a.evidence?`<div class="boost-evidence">${a.evidence}</div>`:''}
      </div>
      <div class="boost-mult">
        <div class="bm-val ${cls}">${isUp?'+':'−'}${pct}%</div>
        <div class="bm-lbl">${a.boost.toFixed(2)}x</div>
      </div>
    </div>`;
  }
  h+=`</div></div>`;

  h+=`</div>`; // end ai-grid

  h+=`<div class="ai-meta">Signals used: WH RSS · WH Schedule · Google News · Truth Social · Google Trends · Historical base rates (${phraseCount} phrases evaluated)</div>`;
  return h;
}

/* ─── Markets tab: grouped by speaker, sorted by EV ─── */
function renderMarkets(){
  const groups=D.groups||[];
  if(!groups.length)return emp('No Markets','Run <b>Start Engine</b> from the Scripts tab, then refresh');

  // SPEAKER_ORDER and SPEAKER_LABELS are global (defined at top of script)

  // Group events by speaker; unknown speakers fall into 'other'
  const bySpk={};
  for(const g of groups){
    const raw=(g.speaker||'other').toLowerCase();
    const key=SPEAKER_ORDER.includes(raw)?raw:'other';
    (bySpk[key]=bySpk[key]||[]).push(g);
  }

  // Sort each speaker's events by best_ev descending (highest edge first)
  for(const k of Object.keys(bySpk)){
    bySpk[k].sort((a,b)=>(parseFloat(b.best_ev)||0)-(parseFloat(a.best_ev)||0));
  }

  const out=SPEAKER_ORDER.filter(s=>bySpk[s]?.length)
    .map(spk=>renderSpeakerSection(spk,bySpk[spk],SPEAKER_LABELS[spk]||spk))
    .join('');
  return out||emp('No Markets','Run <b>Start Engine</b> from the Scripts tab, then refresh');
}

function renderSpeakerSection(spk,events,label){
  const allCards=events.flatMap(g=>g.cards||[]);
  const buyCards=allCards.filter(c=>c.side!=='WATCH');
  const byCount=buyCards.filter(c=>c.side==='BUY_YES').length;
  const bnCount=buyCards.filter(c=>c.side==='BUY_NO').length;
  const totalPhrases=events.reduce((s,g)=>s+(g.total_cards||0),0);
  const allEvs=events.map(g=>parseFloat(g.best_ev)||0).filter(v=>v>0);
  const bestEv=allEvs.length?Math.max(...allEvs):0;

  const _knownAv=new Set(['trump','leavitt','mamdani','fed','ncaab','nba','mlb','mma']);
  const avCls='av-'+(_knownAv.has(spk)?spk:'other');
  const initial=label[0]?.toUpperCase()||'?';

  const bestEvStr=bestEv>0.001
    ?`<span class="spk-ev">best EV +${bestEv.toFixed(3)}</span>`:'';
  const badges=[
    byCount?`<span class="pill pill-yes">${byCount} YES</span>`:'',
    bnCount?`<span class="pill pill-no">${bnCount} NO</span>`:'',
  ].filter(Boolean).join('');

  return `<div class="spk-section spk-${spk}">
    <div class="spk-header">
      <div class="speaker-av spk-av-lg ${avCls}">${initial}</div>
      <div class="spk-info">
        <div class="spk-name">${label}</div>
        <div class="spk-meta">
          <span>${events.length} event${events.length!==1?'s':''}</span>
          <span>${totalPhrases} phrase${totalPhrases!==1?'s':''}</span>
          ${bestEvStr}
        </div>
      </div>
      <div class="spk-badges">${badges}</div>
    </div>
    <div class="spk-body">${events.map(renderGroup).join('')}</div>
  </div>`;
}

/* ─── Sports tab ─── */
const SPORT_ACCENT={nba:'var(--blue)',ncaab:'var(--purple)',mlb:'var(--green)',fight:'#f59e0b'};
const SPORT_ACCENT_RGBA={nba:'59,130,246',ncaab:'168,85,247',mlb:'34,197,94',fight:'245,158,11'};

function renderSports(){
  const sd=D.sports||{};
  const sports=sd.sports||[];

  if(!sports.length){
    return emp('No Sports Markets',
      'Sports markets (NBA, NCAAB, MLB, MMA/UFC) load automatically once the engine is running. For NBA arena certainties run <b>NBA Schedule</b> then <b>NBA Certainties</b> from Scripts.');
  }

  // Aggregate totals across all sports
  let totalGames=0,totalSignals=0,totalArenaOvr=0,totalActive=0;
  for(const sp of sports){
    for(const g of sp.games||[]){
      totalGames++;
      totalSignals+=(g.buy_yes_count||0)+(g.buy_no_count||0);
      totalArenaOvr+=(g.arena_override_count||0);
      if(!g.is_ended)totalActive++;
    }
  }

  let h=`<div class="stats" style="margin-bottom:16px">
    <div class="st"><div class="sl">Active Games</div><div class="sv b">${totalActive}</div><div class="ss">today / upcoming</div></div>
    <div class="st"><div class="sl">Buy Signals</div><div class="sv g">${totalSignals}</div><div class="ss">across all leagues</div></div>
    <div class="st"><div class="sl">Arena Overrides</div><div class="sv p">${totalArenaOvr}</div><div class="ss">near-certain bets</div></div>
    <div class="st"><div class="sl">Total Events</div><div class="sv">${totalGames}</div><div class="ss">${sports.length} league${sports.length!==1?'s':''}</div></div>
  </div>`;

  for(const sp of sports){
    const accent=SPORT_ACCENT[sp.sport]||'var(--text2)';
    const rgba=SPORT_ACCENT_RGBA[sp.sport]||'120,120,120';
    const games=sp.games||[];
    const active=games.filter(g=>!g.is_ended);
    const ended=games.filter(g=>g.is_ended);
    const spSig=games.reduce((s,g)=>s+(g.buy_yes_count||0)+(g.buy_no_count||0),0);

    h+=`<div style="margin-bottom:24px">
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;padding:10px 14px;background:rgba(${rgba},.08);border-radius:8px;border:1px solid rgba(${rgba},.25)">
        <span style="font-size:20px">${sp.icon}</span>
        <div>
          <div style="font-size:14px;font-weight:700;color:${accent}">${sp.label}</div>
          <div style="font-size:11px;color:var(--text3)">${active.length} active · ${ended.length} ended · ${spSig} signal${spSig!==1?'s':''}</div>
        </div>
    </div>`;

  if(active.length){
      h+=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);margin-bottom:6px">Active / Upcoming</div>`;
      h+=active.map(g=>renderSportsGroup(g,sp.icon,sp.sport)).join('');
  }
  if(ended.length){
      h+=`<div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);margin:12px 0 6px">Past Events</div>`;
      h+=ended.map(g=>renderSportsGroup(g,sp.icon,sp.sport)).join('');
  }
    h+=`</div>`;
  }
  return h;
}

function renderSportsGroup(g,icon,sportKey){
  const id='sp-'+g.event_ticker;
  const isOpen=openGroups.has(id);
  const isClosed=g.is_ended||false;
  const closedCls=isClosed?'closed':'';
  const closeBadge=isClosed?'<span class="pill pill-ended">Ended</span>':
    g.game_date?`<span class="pill pill-time">${g.game_date}</span>`:'';

  const bets=(g.cards||[]).filter(c=>c.side!=='WATCH').sort((a,b)=>{
    const ae=a.side==='BUY_YES'?parseFloat(a.ev_yes):parseFloat(a.ev_no);
    const be=b.side==='BUY_YES'?parseFloat(b.ev_yes):parseFloat(b.ev_no);
    return be-ae;
  });
  const watchCards=(g.cards||[]).filter(c=>c.side==='WATCH');

  const byCount=g.buy_yes_count||0;
  const bnCount=g.buy_no_count||0;
  const bestEv=g.best_ev!=null?parseFloat(g.best_ev):null;
  const bestEvStr=bestEv!=null&&bestEv>0.001
    ?`<span style="font-size:11px;font-weight:700;color:${bestEv>=0.05?'var(--green)':'var(--text2)'};font-family:'JetBrains Mono',monospace">best EV +${bestEv.toFixed(3)}</span>`:'';

  const arenaBadge=g.arena_phrase
    ?`<span style="font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px;background:rgba(34,197,94,.12);color:var(--green);border:1px solid rgba(34,197,94,.2);margin-right:6px">🏟 ${g.arena_phrase}</span>`:'';

  const venueLabel=g.arena||(sportKey==='nba'?'NBA':sportKey==='ncaab'?'NCAAB':sportKey==='mlb'?'MLB':'MMA/UFC');

  const bodyContent=bets.length
    ?renderBestBets(bets)
    :`<div style="padding:16px 20px;font-size:12px;color:var(--text3);display:flex;align-items:center;gap:8px">
        <span style="width:6px;height:6px;border-radius:50%;background:var(--border2);flex-shrink:0;display:inline-block"></span>
        ${g.total_cards?`All ${g.total_cards} phrases filtered — run engine to score`:'No phrase markets loaded yet'}
      </div>`;

  const watchSection=watchCards.length
    ?`<div class="watch-toggle" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='none'?'block':'none';this.querySelector('.wt-arrow').textContent=(this.querySelector('.wt-arrow').textContent==='\u25b8'?'\u25be':'\u25b8')">
        <span style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.06em">${watchCards.length} filtered phrase${watchCards.length!==1?'s':''}</span>
        <span class="wt-arrow" style="margin-left:5px">\u25b8</span>
       </div>
       <div class="watch-cards" style="display:none">${renderWatchSection(watchCards)}</div>`
    :'';

  return `<div class="evt-group ${closedCls}">
    <div class="evt-head ${isOpen?'open':''}" onclick="toggleSport('${id}')">
      <div class="left">
        <div class="speaker-av av-nba">${icon}</div>
        <div class="info">
          <h3>${g.label||g.event_ticker}</h3>
          <div class="meta">
            <span>${venueLabel}</span>
            <span>${bets.length} signal${bets.length!==1?'s':''} · ${g.total_cards||0} phrases</span>
            ${bestEvStr}
          </div>
        </div>
      </div>
      <div class="right">
        ${arenaBadge}
        ${closeBadge}
        ${byCount?`<span class="pill pill-yes">${byCount} YES</span>`:''}
        ${bnCount?`<span class="pill pill-no">${bnCount} NO</span>`:''}
        <span class="chevron">&#9660;</span>
      </div>
    </div>
    <div class="evt-body ${isOpen?'open':''}">
      ${bodyContent}
      ${watchSection}
    </div>
  </div>`;
}

function toggleSport(id){
  if(openGroups.has(id))openGroups.delete(id);else openGroups.add(id);
  render();
}

function renderCoverage(){
  const c=D.coverage||{};
  const events=c.events||[];
  if(!events.length){
    return emp('No Coverage Data','Run <b>Fetch Markets</b> from the Scripts tab, then restart engine');
  }

  let h=`<div class="stats" style="margin-bottom:12px">
    <div class="st"><div class="sl">Open Events</div><div class="sv b">${c.open_event_count||0}</div><div class="ss">in snapshots</div></div>
    <div class="st"><div class="sl">Tracked Events</div><div class="sv g">${c.tracked_event_count||0}</div><div class="ss">have action cards</div></div>
    <div class="st"><div class="sl">Untracked Events</div><div class="sv a">${c.untracked_event_count||0}</div><div class="ss">need mapping/score</div></div>
    <div class="st"><div class="sl">Untracked Markets</div><div class="sv a">${c.untracked_market_count||0}</div><div class="ss">open contracts</div></div>
    <div class="st"><div class="sl">Filtered Status</div><div class="sv">${c.filtered_status_market_count||0}</div><div class="ss">not open/active</div></div>
    <div class="st"><div class="sl">Speaker Unresolved</div><div class="sv">${c.filtered_unknown_speaker_market_count||0}</div><div class="ss">visibility risk</div></div>
  </div>`;

  h+=`<div class="cov-table"><table>
    <tr><th>Event</th><th>Speaker</th><th>Open Mkts</th><th>Scored</th><th>Reasons</th><th>Sample Odds</th></tr>`;
  for(const e of events){
    const scored=e.has_scored?'<span class="cov-yes">Yes</span>':'<span class="cov-no">No</span>';
    const reasons=(e.reason_codes||[]).map(covReason).join(' ')||'-';
    const sample=e.sample_market||{};
    const y=sample.yes_ask!=null?'$'+Number(sample.yes_ask).toFixed(2):'-';
    const n=sample.no_ask!=null?'$'+Number(sample.no_ask).toFixed(2):'-';
    h+=`<tr>
      <td class="t-phrase">${e.label||e.event_ticker}<div style="font-size:10px;color:var(--text3)">${e.event_ticker||''}</div></td>
      <td>${e.speaker||'-'}</td>
      <td class="t-num">${e.open_markets||0}</td>
      <td>${scored}</td>
      <td>${reasons}</td>
      <td class="t-num">YES ${y} / NO ${n}</td>
    </tr>`;
  }
  return h+'</table></div>';
}

function covReason(code){
  if(code==='EVENT_DISCOVERED_NO_SNAPSHOTS')return'<span class="cov-reason cov-cache">Event Not Snapshotted</span>';
  if(code==='SPEAKER_UNRESOLVED')return'<span class="cov-reason cov-shape">Speaker Unresolved</span>';
  if(code==='NO_ACTION_CARD')return'<span class="cov-reason cov-miss">No Action Card</span>';
  if(code==='PARTIAL_ACTION_CARD')return'<span class="cov-reason cov-miss">Partial Cards</span>';
  if(code==='NOT_IN_CACHE')return'<span class="cov-reason cov-cache">Not In Cache</span>';
  if(code==='PARTIAL_NOT_IN_CACHE')return'<span class="cov-reason cov-cache">Partial Cache Miss</span>';
  if(code==='NON_PHRASE_MARKET')return'<span class="cov-reason cov-shape">Non-Phrase Market</span>';
  if(code==='PARTIAL_NON_PHRASE')return'<span class="cov-reason cov-shape">Mixed Market Type</span>';
  if(code==='NO_POLY_MATCH')return'<span class="cov-reason cov-poly">No Poly Match</span>';
  if(code==='PARTIAL_POLY_MATCH')return'<span class="cov-reason cov-poly">Partial Poly Match</span>';
  if(code==='NO_WALLET_SIGNAL')return'<span class="cov-reason cov-poly">No Wallet Signal</span>';
  if(code==='PARTIAL_WALLET_SIGNAL')return'<span class="cov-reason cov-poly">Partial Wallet Signal</span>';
  return `<span class="cov-reason">${code}</span>`;
}

function renderGroup(g){
  const id=g.event_ticker;
  const isOpen=openGroups.has(id);
  const isClosed=g.is_closed||false;
  const spk=g.speaker||'other';
  const _knownSpk=new Set(['trump','leavitt','mamdani','fed','ncaab','nba','mlb','mma']);
  const avCls='av-'+(_knownSpk.has(spk)?spk:'other');
  const initial=spk[0]?.toUpperCase()||'M';
  const byCount=g.buy_yes_count||0;
  const bnCount=g.buy_no_count||0;
  const wCount=g.watch_count||0;

  // Actionable bets only — sorted by EV descending
  const bets=g.cards.filter(c=>c.side!=='WATCH').sort((a,b)=>{
    const ae=a.side==='BUY_YES'?parseFloat(a.ev_yes):parseFloat(a.ev_no);
    const be=b.side==='BUY_YES'?parseFloat(b.ev_yes):parseFloat(b.ev_no);
    return be-ae;
  });

  // WATCH cards separate — collapsed by default
  const watchCards=g.cards.filter(c=>c.side==='WATCH');

  const closedCls=isClosed?'closed':'';
  const closeBadge=isClosed?'<span class="pill pill-ended">Ended</span>':
    g.closes_in?`<span class="pill pill-time">${g.closes_in}</span>`:'';

  // Best EV across all bets (for header preview)
  const bestEv=g.best_ev!=null?parseFloat(g.best_ev):null;
  const bestEvStr=bestEv!=null&&bestEv>0.001?`<span style="font-size:11px;font-weight:700;color:${bestEv>=0.05?'var(--green)':'var(--text2)'};font-family:'JetBrains Mono',monospace">best EV +${bestEv.toFixed(3)}</span>`:'';

  // When no bets, show a compact "all filtered" message
  const bodyContent=bets.length
    ?renderBestBets(bets)
    :`<div style="padding:16px 20px;font-size:12px;color:var(--text3);display:flex;align-items:center;gap:8px">
        <span style="width:6px;height:6px;border-radius:50%;background:var(--border2);flex-shrink:0;display:inline-block"></span>
        All ${g.total_cards} phrase${g.total_cards!==1?'s':''} filtered by gates — no actionable signals
      </div>`;

  const watchSection=watchCards.length
    ?`<div class="watch-toggle" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='none'?'block':'none';this.querySelector('.wt-arrow').textContent=(this.querySelector('.wt-arrow').textContent==='▸'?'▾':'▸')">
        <span style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.06em">${watchCards.length} filtered phrase${watchCards.length!==1?'s':''}</span>
        <span class="wt-arrow" style="margin-left:5px">▸</span>
       </div>
       <div class="watch-cards" style="display:none">${renderWatchSection(watchCards)}</div>`
    :'';

  return `<div class="evt-group ${closedCls}">
    <div class="evt-head ${isOpen?'open':''}" onclick="toggleGroup('${id}')">
      <div class="left">
        <div class="speaker-av ${avCls}">${initial}</div>
        <div class="info">
          <h3>${g.label}</h3>
          <div class="meta">
            <span>${spk[0].toUpperCase()+spk.slice(1)}</span>
            <span>${bets.length} signal${bets.length!==1?'s':''} · ${g.total_cards} phrases</span>
            ${bestEvStr}
          </div>
        </div>
      </div>
      <div class="right">
        ${closeBadge}
        ${byCount?`<span class="pill pill-yes">${byCount} YES</span>`:''}
        ${bnCount?`<span class="pill pill-no">${bnCount} NO</span>`:''}
        <span class="chevron">&#9660;</span>
      </div>
    </div>
    <div class="evt-body ${isOpen?'open':''}">
      ${bodyContent}
      ${watchSection}
    </div>
  </div>`;
}

function toggleGroup(id){
  if(openGroups.has(id))openGroups.delete(id);else openGroups.add(id);
  render();
}

function copyHint(el,text){
  navigator.clipboard.writeText(text).then(()=>{
    const orig=el.textContent;el.textContent='Copied!';
    setTimeout(()=>{el.textContent=orig;},1200);
  }).catch(()=>{});
}

function renderBestBets(bets){
  if(!bets.length) return '';
  let h=`<div class="bet-cards">`;
  for(const c of bets){
    const isY=c.side==='BUY_YES';
    const evRaw=isY?parseFloat(c.ev_yes):parseFloat(c.ev_no);
    const ev=c.risk_adjusted_ev_chosen!=null?parseFloat(c.risk_adjusted_ev_chosen):evRaw;
    const evStr=(ev>=0?'+':'')+ev.toFixed(3);
    const evCls=ev>=0.03?'pos':'neg';
    const phrase=(c.phrase&&c.phrase.trim())?c.phrase:'Unknown phrase';
    const yesAsk=parseFloat(c.yes_ask||0);
    const noAsk=parseFloat(c.no_ask||0);
    const polyYes=c.poly_yes!=null?parseFloat(c.poly_yes):null;
    const walletConf=c.wallet_confidence!=null?parseFloat(c.wallet_confidence):null;
    const walletBias=(c.wallet_bias||'').toLowerCase();
    const p=parseFloat(c.p_literal);
    const pCal=c.p_calibrated!=null?parseFloat(c.p_calibrated):p;
    const reasons=(c.reasons||'').split(',').filter(Boolean);
    const hintTxt=c.exec_price_hint||'';
    const klVal=c.kl_divergence!=null?parseFloat(c.kl_divergence):null;
    const klHigh=klVal!=null&&klVal>=0.15;
    const isDirect=!!c.llm_direct_ev;
    const isHighConf=c.llm_confidence==='high';
    const tierA=ev>=0.10&&(isDirect||klHigh||isHighConf);
    const sideCls=isY?'bc-yes':'bc-no';

    // Tags — skip internal gate flags, keep signal-quality ones
    const skipTags=new Set(['GATE_PASS','PRE_EVENT','GATE_FAIL','HIGH_BASE_RATE','PLATT_CALIBRATED','MARKET_ANCHOR','KELLY_WEAK']);
    const tags=reasons.filter(r=>!skipTags.has(r)).slice(0,4).map(tagHtml).filter(Boolean).join(' ');
    const confBit=confirmedBadge(c);
    const histBit=histBadge(c);

    // Poly divergence flag
    let polyFlag='';
    if(polyYes!=null){
      const diff=polyYes-yesAsk;
      if(Math.abs(diff)>=0.08) polyFlag=`<span class="poly-flag">${diff>0?'Poly +':'Poly −'}${Math.abs(diff*100).toFixed(0)}¢</span>`;
    }
    // Wallet tag (only show when meaningful)
    let walletTag='';
    if(walletConf!=null&&walletConf>=0.5){
      const wb=walletBias==='up'?'Wallet↑':walletBias==='down'?'Wallet↓':'Wallet';
      walletTag=`<span class="tag tag-poly">${wb} ${Math.round(walletConf*100)}%</span>`;
    }
    // KL badge
    const klBit=klHigh?`<span style="font-size:9px;font-weight:700;color:var(--amber)">KL ${klVal.toFixed(2)}</span>`:'';

    // LLM insight panel
    let llmBit='';
    if(c.llm_boost&&Math.abs(parseFloat(c.llm_boost)-1.0)>=0.05){
      const boost=parseFloat(c.llm_boost);
      const dir=boost>=1.0?'up':'dn';
      const pct=Math.abs(Math.round((boost-1.0)*100));
      const conf=c.llm_confidence||'';
      const topic=c.llm_topic?`<span style="font-size:9px;background:rgba(168,85,247,.15);color:#c084fc;padding:1px 5px;border-radius:3px;font-weight:600;margin-left:4px">${c.llm_topic}</span>`:'';
      const confBadge=conf?`<span class="llm-conf-badge llm-conf-${conf}">${conf}</span>`:'';
      llmBit=`<div class="bc-llm bc-llm-${dir}">
        <div class="bc-llm-hd">${dir==='up'?'↑':'↓'} AI ${dir==='up'?'Boost':'Suppress'}<span class="bc-llm-pill">${dir==='up'?'+':'−'}${pct}%</span>${topic}${confBadge}</div>
        ${c.llm_reasoning?`<div class="bc-llm-txt">${c.llm_reasoning}</div>`:''}
        ${c.llm_evidence?`<div class="bc-llm-ev">💬 "${c.llm_evidence}"</div>`:''}
      </div>`;
    }

    // Prices pill row
    const yesColor=isY?'color:var(--green)':'color:var(--text2)';
    const noColor=!isY?'color:var(--red)':'color:var(--text2)';
    const divergence=yesAsk-pCal;
    const divergeDir=divergence>0.06?'↑ mkt high':divergence<-0.06?'↓ mkt low':'≈ aligned';
    const divColor=divergence>0.06?'color:var(--red)':divergence<-0.06?'color:var(--green)':'color:var(--text3)';
    let pricesHtml=`<div class="bc-prices">
      <div class="bc-price"><div class="bc-pval" style="${yesColor}">${(yesAsk*100).toFixed(0)}¢</div><div class="bc-plbl">YES</div></div>
      <div class="bc-price"><div class="bc-pval" style="${noColor}">${(noAsk*100).toFixed(0)}¢</div><div class="bc-plbl">NO</div></div>
      <div class="bc-price" title="Calibrated model probability"><div class="bc-pval bc-pval-model">${(pCal*100).toFixed(0)}%</div><div class="bc-plbl" style="${divColor}">${divergeDir}</div></div>`;
    if(polyYes!=null){
      pricesHtml+=`<div class="bc-price"><div class="bc-pval bc-pval-poly">${(polyYes*100).toFixed(0)}¢</div><div class="bc-plbl">Poly</div></div>`;
    }
    pricesHtml+=`</div>`;

    // Action button
    const buyLabel=isY?`Buy YES @ ${(yesAsk*100).toFixed(0)}¢`:`Buy NO @ ${(noAsk*100).toFixed(0)}¢`;
    const actionHtml=hintTxt
      ?`<button class="bc-btn bc-btn-${isY?'yes':'no'}" onclick="copyHint(this,'${hintTxt.replace(/'/g,"\\'")}')">
          ${buyLabel}
        </button>
        <div class="bc-copy-lbl" onclick="copyHint(this,'${hintTxt.replace(/'/g,"\\'")}')">tap to copy ticker</div>`
      :`<span style="font-size:11px;color:var(--text3);padding:4px 0">—</span>`;

    h+=`<div class="bet-card ${sideCls}${tierA?' bc-tier-a':''}">
      <div class="bc-body">
        <div class="bc-top">
          <span class="bc-side-badge">${isY?'Buy YES':'Buy NO'}</span>
          <span class="bc-phrase">"${phrase}"</span>
          ${confBit}
        </div>
        <div class="bc-tags">${tags} ${polyFlag} ${walletTag} ${klBit} ${histBit}</div>
        ${llmBit}
        ${pricesHtml}
      </div>
      <div class="bc-right">
        <div class="bc-ev-wrap">
          <div class="bc-ev-num ${evCls}">${evStr}</div>
          <div class="bc-ev-lbl">EV</div>
          ${klHigh?`<div class="bc-ev-kl">KL ${klVal.toFixed(2)}</div>`:''}
        </div>
        <div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px;width:100%">
          ${actionHtml}
        </div>
      </div>
    </div>`;
  }
  return h+'</div>';
}

function renderAllCardsTable(cards){
  let h=`<div class="all-cards"><table>
    <tr><th>Side</th><th>Phrase</th><th>Status</th><th>History</th><th title="Platt-calibrated probability (hover for raw)">p_cal</th><th>Score</th><th>K. Yes</th><th>K. No</th><th>Poly</th><th>KL</th><th>EV</th><th>Action</th></tr>`;
  for(const c of cards){
    const isY=c.side==='BUY_YES';
    const isN=c.side==='BUY_NO';
    const sideCls=isY?'t-yes':isN?'t-no':'t-w';
    const ev=isY?parseFloat(c.ev_yes):parseFloat(c.ev_no);
    const evStr=(ev>=0?'+':'')+ev.toFixed(3);
    const evCls=ev>=0.03?'pos':'neg';
    const polyStr=c.poly_yes!=null?'$'+parseFloat(c.poly_yes).toFixed(2):'-';
    const polyCls=c.poly_yes!=null?'t-poly':'';
    const scoreConfStr=c.score_confidence!=null?`${Math.round(parseFloat(c.score_confidence)*100)}%`:'-';
    const confTd=c.hit_confirmed?'<span class="tag tag-confirmed">CONFIRMED</span>':'<span style="color:var(--text3);font-size:10px">Pending</span>';
    const histTd=c.hist_total>0?histBadge(c):'<span style="color:var(--text3);font-size:10px">No data</span>';
    // Show calibrated p; tooltip shows raw p_literal for comparison
    const pCal=c.p_calibrated!=null?parseFloat(c.p_calibrated):parseFloat(c.p_literal);
    const pRaw=parseFloat(c.p_literal);
    const calTip=c.p_calibrated!=null?`title="Raw: ${(pRaw*100).toFixed(0)}% → Cal: ${(pCal*100).toFixed(0)}%"`:'';
    const calCls=c.p_calibrated!=null&&Math.abs(pCal-pRaw)>=0.05?'style="color:var(--blue)"':'';
    const klVal=c.kl_divergence!=null?parseFloat(c.kl_divergence):null;
    const klTd=klVal!=null?`<span style="color:${klVal>=0.15?'var(--amber)':klVal>=0.05?'rgba(245,158,11,.6)':'var(--text3)'}">${klVal.toFixed(2)}</span>`:'-';
    h+=`<tr>
      <td><span class="t-side ${sideCls}">${c.side.replace('_',' ')}</span></td>
      <td class="t-phrase">${c.phrase||'-'}</td>
      <td>${confTd}</td>
      <td>${histTd}</td>
      <td class="t-num" ${calTip} ${calCls}>${(pCal*100).toFixed(0)}%</td>
      <td class="t-num">${scoreConfStr}</td>
      <td class="t-num">$${parseFloat(c.yes_ask||0).toFixed(2)}</td>
      <td class="t-num">$${parseFloat(c.no_ask||0).toFixed(2)}</td>
      <td class="t-num ${polyCls}">${polyStr}</td>
      <td class="t-num">${klTd}</td>
      <td class="t-num t-ev ${evCls}">${evStr}</td>
      <td class="t-hint">${c.exec_price_hint||'-'}${c.size_rec?` <span class="sz-badge">$${c.size_rec}</span>`:''}</td>
    </tr>`;
  }
  return h+'</table></div>';
}

function renderWatchSection(cards){
  let h=`<div class="watch-list">`;
  for(const c of cards){
    const ev=Math.max(parseFloat(c.ev_yes),parseFloat(c.ev_no));
    const evStr=(ev>=0?'+':'')+ev.toFixed(3);
    const evCls=ev>=0.03?'pos':'neg';
    const pCal=c.p_calibrated!=null?parseFloat(c.p_calibrated):parseFloat(c.p_literal);
    const pRaw=parseFloat(c.p_literal);
    const calTip=c.p_calibrated!=null?`title="Raw: ${(pRaw*100).toFixed(0)}% → Cal: ${(pCal*100).toFixed(0)}%"`:'';
    const yesAsk=parseFloat(c.yes_ask||0);
    const noAsk=parseFloat(c.no_ask||0);
    const reasons=(c.reasons||'').split(',').filter(Boolean);
    const gateReasons=reasons.filter(r=>!['GATE_PASS','PRE_EVENT','HIGH_BASE_RATE','PLATT_CALIBRATED'].includes(r));
    const reasonTags=gateReasons.slice(0,3).map(tagHtml).filter(Boolean).join(' ');
    h+=`<div class="watch-row">
      <span class="wr-phrase" ${calTip}>${c.phrase||'—'}</span>
      <span class="wr-tags">${reasonTags||''}</span>
      <span class="wr-prices">
        <span style="color:var(--text3);font-size:10px">YES</span> ${(yesAsk*100).toFixed(0)}¢
        <span style="color:var(--text3);font-size:10px;margin-left:4px">Model</span> ${(pCal*100).toFixed(0)}%
      </span>
      <span class="wr-ev ${evCls}">${evStr}</span>
    </div>`;
  }
  return h+'</div>';
}

function tagHtml(r){
  if(r==='NEWS_PRESSURE_HIGH')return'<span class="tag tag-news">News</span>';
  if(r==='X_BUZZ_HIGH')return'<span class="tag tag-news">X Buzz</span>';
  if(r==='POLY_DIVERGE')return'<span class="tag tag-poly">Poly Diverge</span>';
  if(r==='POLY_HIGHER')return'<span class="tag tag-poly">Poly Higher</span>';
  if(r==='POLY_LOWER')return'<span class="tag tag-poly">Poly Lower</span>';
  if(r==='POLY_CONF_HIGH')return'<span class="tag tag-poly">Poly Conf High</span>';
  if(r==='POLY_CONF_MED')return'<span class="tag tag-poly">Poly Conf Med</span>';
  if(r==='POLY_CONF_LOW')return'<span class="tag tag-poly">Poly Conf Low</span>';
  if(r==='POLY_LOW_CONF')return'<span class="tag tag-poly">Poly Not Blended</span>';
  if(r==='POLY_WEAK_MATCH')return'<span class="tag tag-spread">Poly Weak Match</span>';
  if(r==='CROSS_MARKET_ARB')return'<span class="tag" style="background:#22c55e30;color:#22c55e;font-weight:700">CROSS-MKT ARB</span>';
  if(r==='CROSS_MARKET_EDGE')return'<span class="tag" style="background:#22c55e20;color:#22c55e">Cross-Mkt Edge</span>';
  if(r==='WALLET_FLOW_UP')return'<span class="tag tag-poly">Wallet Up</span>';
  if(r==='WALLET_FLOW_DOWN')return'<span class="tag tag-poly">Wallet Down</span>';
  if(r==='WALLET_LOW_CONF')return'<span class="tag tag-poly">Wallet Low Conf</span>';
  if(r==='WALLET_EXTREME_BETS')return'<span class="tag" style="background:#f59e0b30;color:#f59e0b">Extreme Bets</span>';
  if(r==='WALLET_REP_STRONG')return'<span class="tag tag-poly">Rep Strong</span>';
  if(r==='SOURCE_AGREE_UP')return'<span class="tag tag-poly">Sources Agree Up</span>';
  if(r==='SOURCE_AGREE_DOWN')return'<span class="tag tag-poly">Sources Agree Down</span>';
  if(r==='SOURCE_CONFLICT')return'<span class="tag tag-off-topic">Source Conflict</span>';
  if(r==='SOURCE_SINGLE')return'<span class="tag tag-base">Single Source</span>';
  if(r==='SCORE_CONF_HIGH')return'<span class="tag tag-base">Score High</span>';
  if(r==='SCORE_CONF_MED')return'<span class="tag tag-base">Score Med</span>';
  if(r==='SCORE_CONF_LOW')return'<span class="tag tag-spread">Score Low</span>';
  if(r==='ADAPTIVE_THRESHOLD_RAISED')return'<span class="tag tag-spread">Threshold Raised</span>';
  if(r==='HIGH_BASE_RATE')return'<span class="tag tag-base">High Base</span>';
  if(r==='PHRASE_HIT')return'<span class="tag tag-confirmed">CONFIRMED</span>';
  if(r==='MARKET_ANCHOR')return'<span class="tag tag-anchor">Anchored</span>';
  if(r==='ON_TOPIC')return'<span class="tag tag-on-topic">On Topic</span>';
  if(r==='OFF_TOPIC')return'<span class="tag tag-off-topic">Off Topic</span>';
  if(r==='OFF_TOPIC_GUARD')return'<span class="tag tag-off-topic">OffTopic Guard</span>';
  if(r==='YES_EV_FILTER')return'<span class="tag tag-spread">Yes EV Filter</span>';
  if(r==='PENNY_GUARD')return'<span class="tag tag-spread">Penny Guard</span>';
  if(r==='LOW_DEPTH')return'<span class="tag tag-thin">Thin</span>';
  if(r==='WIDE_SPREAD')return'<span class="tag tag-spread">Wide</span>';
  // Calibration + information-theory badges
  if(r==='PLATT_CALIBRATED')return'<span class="tag" style="background:rgba(59,130,246,.12);color:var(--blue)">Calibrated</span>';
  if(r==='KL_HIGH')return'<span class="tag" style="background:rgba(245,158,11,.15);color:var(--amber);font-weight:700">KL High</span>';
  if(r==='KL_MED')return'<span class="tag" style="background:rgba(245,158,11,.08);color:var(--amber)">KL Med</span>';
  if(r==='KELLY_WEAK')return'<span class="gate-badge gate-bearish">Kelly Weak</span>';
  // Gate badges
  if(r==='NO_CONVICTION_FLOOR')return'<span class="gate-badge gate-no-floor">No Conviction</span>';
  if(r==='MARKET_BEARISH_BLOCK')return'<span class="gate-badge gate-bearish">Mkt Bearish</span>';
  if(r==='MARKET_VETO_NO')return'<span class="gate-badge gate-veto-no">Mkt Veto NO</span>';
  if(r==='POLY_VETO_NO')return'<span class="gate-badge gate-poly-veto">Poly Veto NO</span>';
  if(r==='LLM_BOOST_HIGH')return'<span class="tag" style="background:rgba(34,197,94,.15);color:var(--green);font-weight:700">AI ↑↑</span>';
  if(r==='LLM_BOOST')return'<span class="tag" style="background:rgba(34,197,94,.1);color:var(--green)">AI ↑</span>';
  if(r==='LLM_SUPPRESS_HIGH')return'<span class="tag" style="background:rgba(239,68,68,.15);color:var(--red);font-weight:700">AI ↓↓</span>';
  if(r==='LLM_SUPPRESS')return'<span class="tag" style="background:rgba(239,68,68,.1);color:var(--red)">AI ↓</span>';
  if(r==='COOCCUR_BOOST')return'<span class="tag" style="background:rgba(59,130,246,.12);color:var(--blue)">Co-Occur</span>';
  return '';
}

function histBadge(c){
  if(c.hist_total==null||c.hist_total===0)return'';
  const pct=Math.round(c.hist_yes/c.hist_total*100);
  const cls=pct>=60?'hr-high':pct<=30?'hr-low':'';
  return `<span class="hist-rate ${cls}">Said <span class="hr-pct">${c.hist_yes}/${c.hist_total} (${pct}%)</span> historically</span>`;
}

function confirmedBadge(c){
  if(c.hit_confirmed)return'<span class="tag tag-confirmed">CONFIRMED</span>';
  return '';
}

/* ─── Poly vs Kalshi tab ─── */
function renderPoly(){
  const cards=(D.all_cards||[]).filter(c=>c.poly_yes!=null);
  if(!cards.length)return emp('No Polymarket Data','Run <b>Fetch Polymarket</b> from the Scripts tab');
  cards.forEach(c=>{c._diff=parseFloat(c.poly_yes)-parseFloat(c.yes_ask||0)});
  /* strong matches first (sorted by arb edge), then weak matches */
  const strong=cards.filter(c=>c.poly_strong_match);
  const weak=cards.filter(c=>!c.poly_strong_match);
  strong.sort((a,b)=>Math.abs(b._diff)-Math.abs(a._diff));
  weak.sort((a,b)=>Math.abs(b._diff)-Math.abs(a._diff));

  /* Arbitrage summary */
  const arbs=strong.filter(c=>{
    const p=parseFloat(c.poly_yes),k=parseFloat(c.yes_ask||0);
    const cost=p<k?(p+(1-k)):(k+(1-p));
    return (1-cost)>=0.05;
  });
  let h='';
  if(arbs.length){
    h+=`<div style="background:var(--surface2);padding:12px 16px;border-radius:8px;margin-bottom:16px;border:1px solid #22c55e40">
    <h3 style="margin:0 0 8px;color:#22c55e">Cross-Market Opportunities (${arbs.length})</h3>
    <p style="margin:0 0 8px;font-size:12px;color:var(--text2)">Same phrase, strong match. Buy YES on cheaper platform + NO on expensive = guaranteed profit.</p>
    <table><tr><th>Phrase</th><th>Poly YES</th><th>Kalshi YES</th><th>Arb Profit</th><th>Strategy</th></tr>`;
    for(const c of arbs){
      const p=parseFloat(c.poly_yes),k=parseFloat(c.yes_ask||0);
      let cost,strat;
      if(p<k){cost=p+(1-k);strat=`Buy YES Poly @ $${p.toFixed(2)} + NO Kalshi @ $${(1-k).toFixed(2)}`}
      else{cost=k+(1-p);strat=`Buy YES Kalshi @ $${k.toFixed(2)} + NO Poly @ $${(1-p).toFixed(2)}`}
      const profit=1-cost;
      h+=`<tr><td class="t-phrase">${c.phrase||'-'}</td>
        <td class="t-num t-poly">$${p.toFixed(2)}</td>
        <td class="t-num">$${k.toFixed(2)}</td>
        <td class="t-num" style="color:#22c55e;font-weight:600">+$${profit.toFixed(2)}</td>
        <td style="font-size:11px">${strat}</td></tr>`;
    }
    h+='</table></div>';
  }

  h+=`<h3 style="margin:16px 0 8px">Strong Matches (${strong.length})</h3>`;
  h+=`<div class="disc-table"><table>
    <tr><th>Phrase</th><th>Speaker</th><th>Kalshi YES</th><th>Poly YES</th><th>Conf</th><th>Diff</th><th>Signal</th><th>Ticker</th></tr>`;
  for(const c of strong){
    const d=c._diff;const ad=Math.abs(d);
    const ds=(d>=0?'+':'')+d.toFixed(2);
    const dcls=ad>=0.15?'d-big':'';
    let sig='<span style="color:var(--text3)">Aligned</span>';
    if(d>0.15)sig='<span class="d-under">Kalshi underpriced</span>';
    else if(d<-0.15)sig='<span class="d-over">Kalshi overpriced</span>';
    else if(d>0.05)sig='<span style="color:var(--text2)">Slight under</span>';
    else if(d<-0.05)sig='<span style="color:var(--text2)">Slight over</span>';
    h+=`<tr>
      <td class="t-phrase">${c.phrase||'-'}</td>
      <td>${c.speaker||'-'}</td>
      <td class="t-num">$${parseFloat(c.yes_ask||0).toFixed(2)}</td>
      <td class="t-num t-poly">$${parseFloat(c.poly_yes).toFixed(2)}</td>
      <td class="t-num">${c.poly_confidence!=null?Math.round(parseFloat(c.poly_confidence)*100)+'%':'-'}</td>
      <td class="t-num d-diff ${dcls}">${ds}</td>
      <td>${sig}</td>
      <td style="font-size:11px;color:var(--text3)">${c.market_id}</td>
    </tr>`;
  }
  h+='</table></div>';
  return h;
}

/* ─── Outcomes tab ─── */
function renderOutcomes(){
  const o=D.outcomes||{};
  if(!o.total)return emp('No Resolved Outcomes','Run <b>Fetch Outcomes</b> then <b>Record Outcomes</b> from Scripts tab');
  const wr=pct(o.win_rate);
  const wrCls=o.win_rate>=0.55?'g':o.win_rate>=0.45?'a':'r';
  const pnlCls=o.total_pnl>=0?'g':'r';
  const pnlSign=o.total_pnl>=0?'+':'';

  let h=`<div class="stats" style="margin-bottom:16px">
    <div class="st"><div class="sl">Resolved Bets</div><div class="sv b">${o.total}</div><div class="ss">all time</div></div>
    <div class="st"><div class="sl">Win Rate</div><div class="sv ${wrCls}">${wr}</div><div class="ss">${o.wins} wins</div></div>
    <div class="st"><div class="sl">Net P&amp;L</div><div class="sv ${pnlCls}">${pnlSign}$${Math.abs(o.total_pnl).toFixed(2)}</div><div class="ss">realized</div></div>
  </div>`;

  // By side panel
  const bs=o.by_side||{};
  h+=`<div class="oc-grid">`;
  for(const [side,v] of Object.entries(bs)){
    const wrc=v.win_rate>=0.55?'g':v.win_rate>=0.45?'a':'r';
    const pc2=o.total_pnl>=0?'g':'r';
    const sign=v.pnl>=0?'+':'';
    h+=`<div class="oc-block">
      <div class="oc-side-label">${side}</div>
      <div class="oc-stat"><span class="oc-val ${wrc}">${pct(v.win_rate)}</span><span class="oc-lbl">Win Rate</span></div>
      <div class="oc-stat"><span class="oc-val">${v.wins}/${v.total}</span><span class="oc-lbl">Wins</span></div>
      <div class="oc-stat"><span class="oc-val ${v.pnl>=0?'g':'r'}">${sign}$${Math.abs(v.pnl).toFixed(2)}</span><span class="oc-lbl">P&amp;L</span></div>
    </div>`;
  }
  h+=`</div>`;

  // By event type
  const be=o.by_event_type||{};
  if(Object.keys(be).length){
    h+=`<h3 class="oc-section-head">Performance by Event Type</h3>`;
    h+=`<div class="disc-table"><table><tr><th>Event Type</th><th>Win Rate</th><th>Wins / Total</th><th>P&amp;L</th></tr>`;
    const etRows=Object.entries(be).sort((a,b)=>b[1].total-a[1].total);
    for(const [et,v] of etRows){
      const wrc=v.win_rate>=0.55?'g':v.win_rate>=0.45?'a':'r';
      const sign=v.pnl>=0?'+':'';
      h+=`<tr><td class="t-phrase">${et}</td><td class="t-num"><span class="badge-${wrc}">${pct(v.win_rate)}</span></td><td class="t-num">${v.wins}/${v.total}</td><td class="t-num ${v.pnl>=0?'':'t-red'}">${sign}$${Math.abs(v.pnl).toFixed(2)}</td></tr>`;
    }
    h+=`</table></div>`;
  }

  // By phrase
  const bp=o.by_phrase||[];
  if(bp.length){
    h+=`<h3 class="oc-section-head">Phrase Performance (min 2 resolved)</h3>`;
    h+=`<div class="disc-table"><table><tr><th>Phrase</th><th>Win Rate</th><th>Wins / Total</th><th>P&amp;L</th></tr>`;
    for(const v of bp){
      const wrc=v.win_rate>=0.55?'g':v.win_rate>=0.45?'a':'r';
      const sign=v.pnl>=0?'+':'';
      h+=`<tr><td class="t-phrase">"${v.phrase}"</td><td class="t-num"><span class="badge-${wrc}">${pct(v.win_rate)}</span></td><td class="t-num">${v.wins}/${v.total}</td><td class="t-num ${v.pnl<0?'t-red':''}">${sign}$${Math.abs(v.pnl).toFixed(2)}</td></tr>`;
    }
    h+=`</table></div>`;
  }

  // Recent bets
  const rc=o.recent||[];
  if(rc.length){
    h+=`<h3 class="oc-section-head">Recent Bets (last 20)</h3>`;
    h+=`<div class="disc-table"><table><tr><th>Phrase</th><th>Side</th><th>Outcome</th><th>Result</th><th>P&amp;L</th><th>Time</th><th>Event Type</th></tr>`;
    for(const r of rc){
      const oc=r.outcome==='yes'?'YES':'NO';
      const res=r.won?'<span class="cov-yes">WIN</span>':'<span class="cov-no">LOSS</span>';
      const sign=r.pnl>=0?'+':'';
      h+=`<tr><td class="t-phrase">"${r.phrase}"</td><td><span class="side-pill ${r.side==='BUY_YES'?'by':'bn'}">${r.side}</span></td><td>${oc}</td><td>${res}</td><td class="t-num ${r.pnl<0?'t-red':''}">${sign}$${Math.abs(r.pnl).toFixed(2)}</td><td style="font-size:11px">${r.ts}</td><td style="font-size:11px">${r.event_type}</td></tr>`;
    }
    h+=`</table></div>`;
  }
  return h;
}
function pct(v){return v!=null?Math.round(v*100)+'%':'—';}

/* ─── Intelligence tab v2 ─── */
function renderIntelligence(){
  const si=D.signals_intel||{};
  const la=D.llm_analysis||{};
  const intel=D.intelligence||[];
  let h='';

  // ── Aggregate stats for KPI cards ──
  let totalBuys=0, totalWatch=0, totalBoosted=0, totalSuppressed=0, totalPhrases=0;
  const allBuyPhrases=[];
  for(const cat of intel){
    totalPhrases+=cat.total||0;
    totalBuys+=cat.buy_count||0;
    totalWatch+=(cat.total||0)-(cat.buy_count||0);
    totalBoosted+=cat.boosted||0;
    totalSuppressed+=cat.suppressed||0;
    for(const p of cat.phrases||[]){
      if(p.side==='BUY_YES'||p.side==='BUY_NO') allBuyPhrases.push({...p,speaker:cat.label,icon:cat.icon});
    }
  }

  // ── 1. KPI Summary Cards ──
  const sigYaml=si.signals_yaml||{};
  h+=`<div class="int-summary">
    <div class="int-kpi"><div class="kv kv-green">${totalBuys}</div><div class="kl">Buy Signals</div></div>
    <div class="int-kpi"><div class="kv">${totalPhrases}</div><div class="kl">Active Phrases</div></div>
    <div class="int-kpi"><div class="kv kv-purple">${totalBoosted}</div><div class="kl">LLM Boosted</div></div>
    <div class="int-kpi"><div class="kv kv-red">${totalSuppressed}</div><div class="kl">LLM Suppressed</div></div>
  </div>`;

  // ── 2. Pipeline Health (compact dot strip) ──
  const ctx=si.signal_context||{};
  const llmSi=si.llm_analysis||{};
  const wh=si.wh_schedule||{};
  function pipDot(age,w,e){if(age==null)return 'warn';return age<w?'ok':age<e?'warn':'err';}
  function pipAge(age){if(age==null)return '?';return age<60?age+'m':Math.round(age/60)+'h';}
  if(sigYaml.stale||!sigYaml.exists){
    h+=`<div style="background:#7c3a00;color:#ffcc80;border:1px solid #c05800;border-radius:6px;padding:10px 14px;margin-bottom:12px;font-size:12px">
      <b>Signal Warning:</b> ${!sigYaml.exists?'signals.yaml missing — LLM boosts inactive':`LLM signals stale (${sigYaml.age_min}m). Run Analyze Global Signals.`}
    </div>`;
  }
  h+=`<div class="int-pipeline">
    <div class="int-pip-item"><span class="int-pip-dot ${ctx.exists?pipDot(ctx.age_min,30,120):'err'}"></span>Signals ${ctx.exists?pipAge(ctx.age_min):'missing'}</div>
    <div class="int-pip-item"><span class="int-pip-dot ${llmSi.exists?pipDot(llmSi.age_min,60,240):'err'}"></span>LLM ${llmSi.exists?pipAge(llmSi.age_min):'missing'}</div>
    <div class="int-pip-item"><span class="int-pip-dot ${sigYaml.exists?pipDot(sigYaml.age_min,30,60):'err'}"></span>Boosts ${sigYaml.exists?pipAge(sigYaml.age_min):'missing'}</div>
    <div class="int-pip-item"><span class="int-pip-dot ${wh.exists?pipDot(wh.age_min,120,480):'warn'}"></span>WH Sched ${wh.exists?(wh.event_count||0)+' events':'missing'}</div>
    ${ctx.phrase_count?`<div class="int-pip-item" style="margin-left:auto"><span style="font-size:10px;color:var(--text3)">${ctx.phrase_count} phrases · ${sigYaml.boosted||0} up · ${sigYaml.suppressed||0} down${sigYaml.direct_ev?` · ${sigYaml.direct_ev} direct ev`:''}</span></div>`:''}
  </div>`;

  // ── 2b. Upcoming / Recent Events ──
  const whEvents=wh.events||[];
  if(whEvents.length){
    h+=`<div class="int-section-head">Recent Events & Schedule (${whEvents.length})</div>`;
    h+=`<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px;margin-bottom:16px">`;
    const evTypeColors={briefing:'var(--blue)',signing:'var(--amber)',remarks:'var(--green)',announcement:'var(--purple)',general:'var(--text3)'};
    for(const ev of whEvents.slice(0,8)){
      const c=evTypeColors[ev.event_type]||'var(--text3)';
      const src=ev.source==='whitehouse'?'WH.gov':'News';
      let ago='';
      if(ev.published_at){try{const d=new Date(ev.published_at);const h2=Math.round((Date.now()-d)/3600000);ago=h2<24?h2+'h ago':Math.round(h2/24)+'d ago';}catch(e){}}
      h+=`<div style="background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:10px 12px;font-size:12px">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
          <span style="font-size:9px;font-weight:700;padding:1px 6px;border-radius:3px;background:${c.replace(')',', .12)').replace('var(','rgba(').replace('--blue','59,130,246').replace('--amber','245,158,11').replace('--green','34,197,94').replace('--purple','168,85,247').replace('--text3','113,113,122')};color:${c}">${(ev.event_type||'event').toUpperCase()}</span>
          <span style="font-size:10px;color:var(--text3)">${src} · ${ago}</span>
        </div>
        <div style="font-size:12px;color:var(--text);line-height:1.4;font-weight:500">${(ev.title||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')}</div>
      </div>`;
    }
    h+=`</div>`;
  }

  // ── 2c. Model Health – Calibration Buckets ──
  const cb=D.calib_buckets||{};
  const cbBuckets=cb.buckets||[];
  if(cbBuckets.length){
    const totalN=cb.total||0;
    const overallYes=cb.overall_yes_pct!=null?cb.overall_yes_pct+'%':'—';
    h+=`<div class="int-section-head">Model Health — Calibration Accuracy (last ${totalN} outcomes · actual YES ${overallYes})</div>`;
    h+=`<div style="overflow-x:auto;margin-bottom:16px"><table class="int-tbl">
      <thead><tr><th>Model p bucket</th><th class="c">N</th><th class="r">Actual YES%</th><th class="r">Model avg p</th><th class="r">Gap (pp)</th><th>Quality</th></tr></thead><tbody>`;
    for(const b of cbBuckets){
      if(!b.n){continue;}
      const gap=b.gap_pp;
      let qLabel='', qColor='var(--green)';
      if(Math.abs(gap)<=5){qLabel='✓ Good';qColor='var(--green)';}
      else if(Math.abs(gap)<=15){qLabel='⚠ Off';qColor='var(--amber)';}
      else{qLabel='✗ Bad';qColor='var(--red)';}
      const gapStr=gap>0?`+${gap.toFixed(1)}`:gap.toFixed(1);
      const gapColor=Math.abs(gap)<=5?'var(--text3)':Math.abs(gap)<=15?'var(--amber)':'var(--red)';
      h+=`<tr>
        <td>${b.label}</td>
        <td class="c">${b.n}</td>
        <td class="r">${b.actual_pct!=null?b.actual_pct+'%':'—'}</td>
        <td class="r">${b.model_pct!=null?b.model_pct+'%':'—'}</td>
        <td class="r" style="color:${gapColor};font-weight:600">${gapStr}pp</td>
        <td style="color:${qColor};font-size:11px;font-weight:600">${qLabel}</td>
      </tr>`;
    }
    h+=`</tbody></table></div>`;
  }

  // ── 3. Top Opportunities (BUY signals across all speakers) ──
  if(allBuyPhrases.length){
    allBuyPhrases.sort((a,b)=>{
      const aEv=a.side==='BUY_YES'?(a.ev_yes||0):(a.ev_no||0);
      const bEv=b.side==='BUY_YES'?(b.ev_yes||0):(b.ev_no||0);
      return bEv-aEv;
    });
    h+=`<div class="int-section-head">Top Opportunities (${allBuyPhrases.length} buy signals)</div>`;
    h+=`<div style="overflow-x:auto;margin-bottom:16px"><table class="int-tbl">
      <thead><tr><th>Phrase</th><th>Speaker</th><th class="c">Side</th><th class="r">Prob</th><th class="r">Ask</th><th class="r">EV</th><th class="c">LLM</th></tr></thead><tbody>`;
    for(const p of allBuyPhrases){
      const side=p.side==='BUY_YES'?'YES':'NO';
      const sideCls=p.side==='BUY_YES'?'pill-yes':'pill-no';
      const ask=p.side==='BUY_YES'?(p.yes_ask!=null?p.yes_ask.toFixed(2):'—'):(p.no_ask!=null?p.no_ask.toFixed(2):'—');
      const ev=p.side==='BUY_YES'?(p.ev_yes||0):(p.ev_no||0);
      const evCls=ev>0?'int-ev-pos':'int-ev-neg';
      const llmVal=p.llm_boost!=null?Math.round((p.llm_boost-1)*100):null;
      const llmStr=llmVal!=null?(llmVal>=0?`<span class="int-llm-up">+${llmVal}%</span>`:`<span class="int-llm-dn">${llmVal}%</span>`):'<span class="int-llm-nil">—</span>';
      h+=`<tr class="buy-row">
        <td class="mono">"${p.phrase}"</td>
        <td style="font-size:11px">${p.icon} ${p.speaker}</td>
        <td class="c"><span class="pill ${sideCls}" style="font-size:10px">${side}</span></td>
        <td class="r mono">${p.p_literal!=null?p.p_literal.toFixed(2):'—'}</td>
        <td class="r mono">${ask}</td>
        <td class="r mono"><span class="${evCls}">${ev>=0?'+':''}${ev.toFixed(2)}</span></td>
        <td class="c">${llmStr}</td>
      </tr>`;
    }
    h+=`</tbody></table></div>`;
  }

  // ── 4. Today's Topics ──
    const topics=la.topics||[];
  if(topics.length){
    h+=`<div class="int-section-head">Today's Topics</div>`;
    h+=`<div class="int-topics">`;
    for(const t of topics){
      const probCls=t.probability==='high'?'high':t.probability==='medium'?'med':'low';
      const phrases=t.likely_phrases||[];
      h+=`<div class="int-topic">
        <div class="int-topic-head">
          <span class="int-topic-name">${t.topic||''}</span>
          <span class="int-topic-prob ${probCls}">${(t.probability||'').toUpperCase()}</span>
      </div>
        ${t.evidence?`<div class="int-topic-ev">${t.evidence}</div>`:''}
        ${phrases.length?`<div class="int-topic-phrases">${phrases.map(p=>`<span class="int-topic-pill">${p}</span>`).join('')}</div>`:''}
    </div>`;
    }
    h+=`</div>`;
  }

  // ── 5. Market Intelligence by Speaker ──
  if(intel.length){
    const configuredModel=la._signal_model||la.model||'';
    const phraseCount=la.phrases_evaluated||0;
    const ageLabel=la.analyzed_at?timeSince(la.analyzed_at)+' ago':'never';
    h+=`<div style="display:flex;align-items:center;justify-content:space-between;margin:0 0 12px;flex-wrap:wrap;gap:6px">
      <div class="int-section-head" style="margin:0;border:0;padding:0">Market Intelligence</div>
      <span style="font-size:11px;color:var(--text3)">${phraseCount} phrases · updated ${ageLabel}${configuredModel?` · <span class="ai-model-badge">${configuredModel}</span>`:''}</span>
      </div>`;

    const sorted=[...intel].sort((a,b)=>{
      const aS=(a.buy_count>0?2:0)+(a.boosted+a.suppressed>0?1:0);
      const bS=(b.buy_count>0?2:0)+(b.boosted+b.suppressed>0?1:0);
      return bS-aS;
    });

    for(const cat of sorted){
      if(!cat.total)continue;
      const hasBuy=cat.buy_count>0;
      const catId='intel-cat-'+cat.category;
      const isOpen=openGroups.has(catId)||hasBuy;
      h+=`<div class="int-cat">
        <div class="int-cat-head${hasBuy?' has-buy':''}" onclick="toggleIntelCat('${catId}')">
          <div class="int-cat-left">
            <span class="int-cat-icon">${cat.icon}</span>
            <div>
              <div class="int-cat-name">${cat.label}</div>
              <div class="int-cat-sub">${cat.total} phrase${cat.total!==1?'s':''}${cat.boosted?` · <span style="color:var(--green)">${cat.boosted} boosted</span>`:''}${cat.suppressed?` · <span style="color:var(--red)">${cat.suppressed} suppressed</span>`:''}</div>
        </div>
          </div>
          <div class="int-cat-badges">
            ${cat.buy_count?`<span class="int-buy-badge">${cat.buy_count} BUY</span>`:''}
            <span style="color:var(--text3);font-size:12px">${isOpen?'▾':'▸'}</span>
        </div>
      </div>`;

      if(isOpen){
        h+=`<div style="overflow-x:auto"><table class="int-tbl">
          <thead><tr><th>Phrase</th><th class="c">Side</th><th class="r">Prob</th><th class="r">Ask</th><th class="r">EV</th><th class="c">LLM</th><th class="c">Conf</th></tr></thead><tbody>`;
        for(const p of cat.phrases){
          const isBuy=p.side==='BUY_YES'||p.side==='BUY_NO';
          const sideCls=p.side==='BUY_YES'?'pill-yes':p.side==='BUY_NO'?'pill-no':'pill-watch';
          const sideLabel=p.side==='BUY_YES'?'YES':p.side==='BUY_NO'?'NO':'WATCH';
          const llmVal=p.llm_boost!=null?Math.round((p.llm_boost-1)*100):null;
          const llmStr=llmVal!=null?(llmVal>=0?`<span class="int-llm-up">+${llmVal}%</span>`:`<span class="int-llm-dn">${llmVal}%</span>`):'<span class="int-llm-nil">—</span>';
          const confCls=p.llm_confidence==='high'?'int-conf-h':p.llm_confidence==='medium'?'int-conf-m':p.llm_confidence==='low'?'int-conf-l':'';
          const confStr=p.llm_confidence?`<span class="${confCls}" style="font-size:10px">${p.llm_confidence.toUpperCase()}</span>`:'<span style="color:var(--text3)">—</span>';
          const scConf=p.score_conf?` <span style="font-size:9px;color:var(--text3)">${p.score_conf}</span>`:'';
          const ev=p.side==='BUY_YES'?(p.ev_yes||0):p.side==='BUY_NO'?(p.ev_no||0):Math.max(p.ev_yes||0,p.ev_no||0);
          const evCls=ev>0?'int-ev-pos':'int-ev-neg';
          const ask=p.side==='BUY_YES'?(p.yes_ask!=null?p.yes_ask.toFixed(2):'—'):(p.no_ask!=null?p.no_ask.toFixed(2):'—');
          const pLit=p.p_literal!=null?p.p_literal.toFixed(2):'—';
          const hasMeta=p.llm_reasoning||p.llm_evidence||p.llm_topic;
          const rowId='ir-'+cat.category+'-'+encodeURIComponent(p.phrase);
          h+=`<tr class="${isBuy?'buy-row':''}" style="cursor:${hasMeta?'pointer':'default'}" ${hasMeta?`onclick="toggleIntelRow('${rowId}')"`:''}>
            <td class="mono">"${p.phrase||''}"${scConf}</td>
            <td class="c"><span class="pill ${sideCls}" style="font-size:10px">${sideLabel}</span></td>
            <td class="r mono">${pLit}</td>
            <td class="r mono">${ask}</td>
            <td class="r mono"><span class="${evCls}">${ev>=0?'+':''}${ev.toFixed(2)}</span></td>
            <td class="c">${llmStr}</td>
            <td class="c">${confStr}</td>
          </tr>`;
          if(hasMeta){
            h+=`<tr id="${rowId}" class="detail-row" style="display:none">
              <td colspan="7">
                ${p.llm_topic?`<div class="int-detail-topic">Topic: ${p.llm_topic}</div>`:''}
                ${p.llm_reasoning?`<div class="int-detail-reason">${p.llm_reasoning}</div>`:''}
                ${p.llm_evidence?`<div class="int-detail-evidence">${p.llm_evidence}</div>`:''}
                ${p.llm_direct?`<span class="int-direct-badge">Direct Evidence</span>`:''}
              </td>
            </tr>`;
          }
        }
        h+=`</tbody></table></div>`;
      }
      h+=`</div>`;
    }
  } else {
    h+=`<div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:20px;margin:16px 0;text-align:center;color:var(--text3)">
      <div style="font-size:14px;font-weight:600;margin-bottom:4px">No active markets</div>
      <div style="font-size:12px">Start the engine and refresh to see intelligence data.</div>
    </div>`;
  }

  // ── 6. Truth Social Feed ──
  const posts=si.truth_social||[];
  if(posts.length){
    h+=`<div class="int-section-head">Truth Social (${posts.length} posts)</div>
    <div class="ts-feed">`;
    for(const p of posts){
      const ageTxt=p.age_h==null?'?':p.age_h<2?`${(p.age_h*60).toFixed(0)}m ago`:p.age_h<48?`${p.age_h.toFixed(1)}h ago`:`${(p.age_h/24).toFixed(1)}d ago`;
      const boostCls=p.boost>=1.35?'boost-high':p.boost>=1.10?'boost-mid':'boost-low';
      h+=`<div class="ts-card">
        <div class="ts-card-top">
          <span class="ts-age">${ageTxt}</span>
          <span class="ts-boost ${boostCls}">${p.boost.toFixed(2)}x</span>
          ${p.url?`<a class="ts-url" href="${p.url}" target="_blank" rel="noopener">view</a>`:''}
        </div>
        <div class="ts-content">${(p.content||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>
      </div>`;
    }
    h+=`</div>`;
  }

  // ── 7. Drift Alerts ──
  const da=si.drift_alerts||[];
  if(da.length){
    h+=`<div class="int-section-head">Drift Alerts (${da.length})</div>
    <div style="overflow-x:auto"><table class="int-tbl">
      <thead><tr><th>Phrase</th><th>Alert</th><th class="r">Value</th><th>Time</th></tr></thead><tbody>`;
    for(const a of da){
      h+=`<tr><td class="mono">${a.phrase||'-'}</td><td>${a.type||a.alert||'-'}</td><td class="r mono">${a.value!=null?a.value:'-'}</td><td style="font-size:11px">${(a.ts||a.time||'').toString().slice(0,16)}</td></tr>`;
    }
    h+=`</tbody></table></div>`;
  }

  return h;
}

function toggleIntelCat(id){
  if(openGroups.has(id))openGroups.delete(id);else openGroups.add(id);
  render();
}

function toggleIntelRow(id){
  const el=document.getElementById(id);
  if(el)el.style.display=el.style.display==='none'?'table-row':'none';
}

function renderPerformance(){
  const o=D.outcomes||{};
  const canon=o.canonical_speakers||SPEAKER_ORDER;
  const bySpk=o.by_speaker||{};
  const pt=D.phrase_trends||{};
  const ptBy=pt.by_speaker||{};
  const rolling=o.rolling||{};

  const WC=wr=>wr>=0.60?'#4ade80':wr>=0.50?'#facc15':'#f87171';
  const WB=(wr,n)=>n?`<span class="badge-${wr>=0.60?'g':wr>=0.50?'a':'r'}">${pct(wr)}</span>`:'<span style="color:#444">—</span>';
  const PC=v=>v>=0?'#4ade80':'#f87171';
  const PS=v=>`${v>=0?'+':''}\$${Math.abs(v).toFixed(2)}`;
  const BSC=v=>v==null?'#555':v>0.05?'#4ade80':v>0?'#facc15':'#f87171';
  const BSD=v=>v==null?'—':`${v>=0?'+':''}${v.toFixed(4)}`;

  let h='';
  if(!(o.total>0)){
    h+=`<div class="pi-perf-banner">No bet journal rows yet — run <b>Fetch Outcomes</b> then <b>Record Outcomes</b>. Every mention-market speaker still appears below so you can track Kalshi vocabulary and plug in P&amp;L as it arrives.</div>`;
  }

  const r30=rolling['30d']||{};
  const allBss=r30.bss;
  const bssTop=Object.entries(bySpk).filter(([_,v])=>v&&v.bss!=null&&(v.total||0)>0).sort((a,b)=>b[1].bss-a[1].bss)[0];
  h+=`<div class="pi-hero">
    <div class="pi-hero-cell">
      <div class="pi-hero-num">${o.total||0}</div>
      <div class="pi-hero-lbl">Journal bets</div>
    </div>
    <div class="pi-hero-cell">
      <div class="pi-hero-num" style="color:${WC(o.win_rate||0)}">${o.total?pct(o.win_rate):'—'}</div>
      <div class="pi-hero-lbl">Win rate</div>
    </div>
    <div class="pi-hero-cell">
      <div class="pi-hero-num" style="color:${PC(o.total_pnl||0)}">${o.total?PS(o.total_pnl||0):'—'}</div>
      <div class="pi-hero-lbl">Total P&amp;L</div>
    </div>
    <div class="pi-hero-cell">
      <div class="pi-hero-num" style="color:${BSC(allBss)}">${allBss!=null?(allBss>=0?'+':'')+allBss.toFixed(4):'—'}</div>
      <div class="pi-hero-lbl">BSS (30d, all)</div>
    </div>
    <div class="pi-hero-cell">
      <div class="pi-hero-num" style="font-size:${bssTop?'14':'22'}px;padding-top:${bssTop?'6':'0'}px;line-height:1.2">${bssTop?(SPEAKER_LABELS[bssTop[0]]||bssTop[0]):'—'}</div>
      <div class="pi-hero-lbl">Best BSS (with bets)</div>
    </div>
  </div>`;

  const sk=window._perfSort||'bss';
  const rankRows=canon.map(spk=>[spk,bySpk[spk]||{}]).sort((a,b)=>{
    const va=a[1],vb=b[1];
    if(sk==='wr')return (vb.win_rate||0)-(va.win_rate||0);
    if(sk==='pnl')return (vb.total_pnl||0)-(va.total_pnl||0);
    if(sk==='bets')return (vb.total||0)-(va.total||0);
    return (vb.bss??-99)-(va.bss??-99);
  });

  h+=`<div class="pi-card" style="margin-bottom:18px">
    <div class="pi-rank-bar">
      <span class="pi-card-title" style="border:0;padding:0;background:transparent;margin:0">Speaker rank</span>
      <div class="pi-rank-sort">
        <button type="button" class="${sk==='bss'?'on':''}" onclick="window._perfSort='bss';render()">BSS</button>
        <button type="button" class="${sk==='pnl'?'on':''}" onclick="window._perfSort='pnl';render()">P&amp;L</button>
        <button type="button" class="${sk==='wr'?'on':''}" onclick="window._perfSort='wr';render()">Win%</button>
        <button type="button" class="${sk==='bets'?'on':''}" onclick="window._perfSort='bets';render()">Bets</button>
      </div>
    </div>
    <div class="pi-rank-strip">`;
  for(const [spk,v] of rankRows){
    const lbl=SPEAKER_LABELS[spk]||spk;
    const short=perfShortLabel(spk,lbl);
    const muted=(v.total||0)===0;
    const bssS=v.bss!=null?(v.bss>=0?'+':'')+v.bss.toFixed(3):'—';
    h+=`<button type="button" class="pi-rank-chip${muted?' muted':''}" onclick="perfScrollToSpk('${spk}')">
      <div class="rn">${short}</div>
      <div class="rs">${muted?'No bets':(v.total+' bet'+(v.total!==1?'s':''))}<br>BSS ${bssS}</div>
    </button>`;
  }
  h+=`</div></div>`;
  if(pt.computed_at){
    h+=`<div style="font-size:10px;color:var(--text3);margin:-4px 0 16px 2px">Phrase trends file: ${pt.computed_at.slice(0,10)} (Kalshi outcomes, not journal)</div>`;
  }

  const knownAv=new Set(['trump','leavitt','mamdani','fed','ncaab','nba','mlb','mma']);

  for(const spk of canon){
    const v=bySpk[spk]||{};
    const lbl=SPEAKER_LABELS[spk]||spk;
    const avCls=knownAv.has(spk)?`av-${spk}`:'av-auto';
    const initial=lbl.split(' ').map(w=>w[0]).join('').slice(0,2).toUpperCase();
    const spRolling=v.rolling||{};
    const phrases=v.top_phrases||[];
    const rc=v.recent||[];
    const bySide=v.by_side||{};
    const barW=Math.round((v.win_rate||0)*100);
    const ptBs=ptBy[spk]||{movers:[]};
    const movers=ptBs.movers||[];

    h+=`<div class="spk-section spk-${spk}" id="perf-spk-${spk}">
      <div class="spk-header">
        <div class="speaker-av spk-av-lg ${avCls}">${initial}</div>
        <div class="spk-info">
          <div class="spk-name">${lbl}</div>
          <div class="spk-meta">
            <span style="font-weight:700;color:${WC(v.win_rate||0)}">${v.total?pct(v.win_rate||0):'—'}</span>
            <span>${v.total?v.wins+'/'+v.total+' journal':'No journal rows'}</span>
            <span style="color:${PC(v.total_pnl||0)};font-weight:700">${v.total?PS(v.total_pnl||0)+' P&amp;L':'—'}</span>
            <span style="color:${BSC(v.bss)};font-weight:700">${v.total?'BSS '+BSD(v.bss):'BSS —'}</span>
          </div>
          <div style="margin-top:8px;max-width:280px;height:4px;background:var(--surface3);border-radius:2px;overflow:hidden">
            <div style="width:${v.total?barW:0}%;height:100%;background:${WC(v.win_rate||0)};border-radius:2px"></div>
          </div>
        </div>
      </div>
      <div class="spk-body pi-perf-body">
        <div class="pi-grid2">
          <div class="pi-card" style="margin-bottom:0">
            <div class="pi-card-head">
              <span class="pi-card-title">Rolling performance</span>
              <span style="font-size:10px;color:var(--text3)">Your journal · BSS vs market</span>
            </div>
            <table class="pi-win-table"><tbody>
              <tr><th>Window</th><th>Bets</th><th>Win%</th><th>P&amp;L</th><th>BSS</th></tr>`;
    for(const w of['7d','30d','60d','90d']){
      const rw=spRolling[w]||{};
      if(!rw.n){h+=`<tr><td>${w}</td><td colspan="4" style="color:#444;font-style:italic">No data</td></tr>`;continue;}
      h+=`<tr><td>${w}</td><td>${rw.n}</td><td>${WB(rw.win_rate,rw.n)}</td>
        <td style="font-weight:700;color:${PC(rw.pnl||0)}">${PS(rw.pnl||0)}</td>
        <td style="font-weight:600;color:${BSC(rw.bss)}">${BSD(rw.bss)}</td></tr>`;
    }
    h+=`<tr><td>All time</td><td>${v.total||0}</td><td>${WB(v.win_rate,v.total)}</td>
      <td style="font-weight:700;color:${PC(v.total_pnl||0)}">${v.total?PS(v.total_pnl||0):'—'}</td>
      <td style="font-weight:600;color:${BSC(v.bss)}">${v.total?BSD(v.bss):'—'}</td></tr>
      </tbody></table>`;
    const sidesHTML=Object.entries(bySide).map(([side,sl])=>{
      const slPnl=sl.pnl||0;
      return `<div class="pi-side-card">
        <div class="pi-side-lbl">${side==='BUY_YES'?'Buy YES':'Buy NO'}</div>
        <div class="pi-side-wr" style="color:${WC(sl.win_rate||0)}">${pct(sl.win_rate||0)}</div>
        <div class="pi-side-sub">${sl.wins}/${sl.total} · <span style="color:${PC(slPnl)}">${PS(slPnl)}</span></div>
      </div>`;
    }).join('');
    if(sidesHTML)h+=`<div class="pi-sides">${sidesHTML}</div>`;
    h+=`</div>
          <div class="pi-card" style="margin-bottom:0">
            <div class="pi-card-head">
              <span class="pi-card-title">Recent activity</span>
              <span style="font-size:10px;color:var(--text3)">${rc.length?('last '+rc.length):'empty'}</span>
            </div>
            <div class="pi-feed" style="max-height:280px;overflow-y:auto">`;
    if(rc.length){
      for(const r of rc){
        const sideShort=r.side==='BUY_YES'?'YES':'NO';
        const outShort=r.outcome==='yes'?'YES':'NO';
        h+=`<div class="pi-feed-row">
          <span class="pi-feed-pill ${r.won?'pi-pill-win':'pi-pill-loss'}">${r.won?'WIN':'LOSS'}</span>
          <span class="pi-feed-phrase">"${r.phrase}"</span>
          <span style="font-size:10px;color:#555;white-space:nowrap">${sideShort}→${outShort}</span>
          <span class="pi-feed-pnl" style="color:${PC(r.pnl)}">${PS(r.pnl)}</span>
          <span class="pi-feed-time">${r.ts.length>10?r.ts.slice(5,16):r.ts}</span>
        </div>`;
      }
    } else {
      h+=`<div style="padding:20px;color:#555;font-style:italic;font-size:12px;text-align:center">No journal resolutions for this speaker yet.</div>`;
    }
    h+=`</div></div></div>`;

    h+=`<div class="pi-perf-phrases">
      <span class="pi-card-title">Phrase breakdown (journal)</span>
      <div style="margin-top:4px">`;
    if(phrases.length){
      for(const ph of phrases){
        h+=`<div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--border1);font-size:12px">
          <span style="flex:1;min-width:0;color:var(--text2);font-style:italic;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">"${ph.phrase}"</span>
          <span style="font-weight:700;color:${WC(ph.win_rate)};width:40px;text-align:right">${pct(ph.win_rate)}</span>
          <span style="color:var(--text3);width:44px;text-align:right">${ph.wins}/${ph.total}</span>
          <span style="font-weight:700;color:${PC(ph.pnl)};width:64px;text-align:right">${PS(ph.pnl)}</span>
        </div>`;
      }
    } else {
      h+=`<div style="font-size:12px;color:#555;font-style:italic">No phrase stats until this speaker has journal entries.</div>`;
    }
    h+=`</div>`;

    h+=`<div class="pi-vocab">
      <span class="pi-card-title">Vocabulary (30d vs 90d)</span>
      <p style="font-size:10px;color:var(--text3);margin:4px 0 8px">Resolved Kalshi mention markets for this speaker — independent of your bet journal.</p>`;
    if(movers.length){
      h+=`<table class="pi-vocab-table"><thead><tr><th>Phrase</th><th>30d</th><th>90d</th><th>Δ</th><th>Flag</th></tr></thead><tbody>`;
      for(const row of movers.slice(0,12)){
        const r30v=row.rates?.['30d'], r90v=row.rates?.['90d'];
        const r30t=r30v!=null?Math.round(r30v*100)+'%':'—';
        const r90t=r90v!=null?Math.round(r90v*100)+'%':'—';
        const td=row.trend_delta;
        const dpp=td!=null?(td>=0?'+':'')+Math.round(td*100)+'pp':'—';
        const fl=row.trend_flag==='VOCAB_TRENDING_UP'?'↑ up':row.trend_flag==='VOCAB_TRENDING_DOWN'?'↓ down':'';
        const dcol=td==null?'var(--text3)':td>0.05?'#4ade80':td<-0.05?'#f87171':'var(--text2)';
        h+=`<tr>
          <td>"${row.phrase}"</td>
          <td>${r30t}</td><td>${r90t}</td>
          <td style="font-weight:700;color:${dcol}">${dpp}</td>
          <td style="font-size:10px;color:var(--text3)">${fl}</td>
        </tr>`;
      }
      h+=`</tbody></table>`;
    } else {
      h+=`<div class="pi-vocab-empty">No Kalshi outcome history for this speaker (or run <b>Phrase Trends</b> in Scripts → Calibration).</div>`;
    }
    h+=`</div></div></div>`;
  }

  return h;
}

/* ─── Analysis tab (Coverage + Poly vs Kalshi with sub-tabs) ─── */
let analysisSubTab='coverage';
function bindAnalysis(){
  document.querySelectorAll('.ana-tab').forEach(el=>{
    el.addEventListener('click',()=>{
      analysisSubTab=el.dataset.a;
      document.querySelectorAll('.ana-tab').forEach(t=>t.classList.toggle('on',t.dataset.a===analysisSubTab));
      document.getElementById('ana-content').innerHTML=analysisSubTab==='coverage'?renderCoverage():renderPoly();
    });
  });
}
function renderAnalysis(){
  return `<div>
    <div class="ana-tabs">
      <div class="ana-tab${analysisSubTab==='coverage'?' on':''}" data-a="coverage">Coverage Diagnostics</div>
      <div class="ana-tab${analysisSubTab==='poly'?' on':''}" data-a="poly">Poly vs Kalshi</div>
    </div>
    <div id="ana-content">${analysisSubTab==='coverage'?renderCoverage():renderPoly()}</div>
  </div>`;
}

/* ─── System tab ─── */
function renderSystem(){
  const h=D.health;if(!h)return emp('No Data','');

  function row(label,value,status){
    const dc=status==='ok'?'hd-ok':status==='warn'?'hd-warn':status==='err'?'hd-err':'hd-info';
    const vStr=(value===null||value===undefined)?'—':String(value);
    return `<div class="h-item-v2"><span class="hd ${dc}"></span><span class="hl">${label}</span><span class="hv">${vStr}</span></div>`;
  }
  function section(title,hint,rows,full=false){
    return `<div class="sys-section${full?' sys-full':''}">
      <div class="sys-section-head">${title}</div>
      ${hint?`<div class="sys-fix-hint">${hint}</div>`:''}
      <div class="sys-list">${rows}</div>
    </div>`;
  }
  function snapStatus(v){return v>0?'ok':v===0?'warn':'err';}
  function ageStatus(iso){
    if(!iso||iso==='-')return 'err';
    try{const m=(Date.now()-new Date(iso).getTime())/60000;return m<30?'ok':m<120?'warn':'err';}catch(e){return 'err';}
  }
  function ageMinStatus(m,warnMins,errMins){
    if(m===null||m===undefined)return 'err';
    return m<warnMins?'ok':m<errMins?'warn':'err';
  }
  function fmtAge(m){
    if(m===null||m===undefined)return '—';
    if(m<60)return `${Math.round(m)}m ago`;
    return `${(m/60).toFixed(1)}h ago`;
  }

  // ── Engine Status ─────────────────────────────────────────────────────────
  const snapAge=h['Latest snapshot'];const cardAge=h['Latest card'];
  const livePulse=ageStatus(snapAge);
  const runnerSt=h['Runner: status']||'';
  const runnerAlive=runnerSt==='alive';
  let engineLabel=livePulse==='ok'
    ?'<span style="color:var(--green);font-weight:700">● Running</span>'
    :livePulse==='warn'
      ?'<span style="color:var(--amber);font-weight:700">⚠ Stale</span>'
      :'<span style="color:var(--red);font-weight:700">✕ Offline</span>';
  let engineRows='';
  engineRows+=row('Engine pulse',engineLabel,livePulse);
  engineRows+=row('Runner process',runnerSt+(h['Runner: PID']?` (PID ${h['Runner: PID']})`:''),runnerAlive?'ok':'err');
  engineRows+=row('Snapshots (48h)',h['Snapshots (48h)'],snapStatus(h['Snapshots (48h)']));
  engineRows+=row('Latest snapshot',snapAge,ageStatus(snapAge));
  engineRows+=row('Action cards (48h)',h['Action cards (48h)'],snapStatus(h['Action cards (48h)']));
  engineRows+=row('Latest card',cardAge,ageStatus(cardAge));
  engineRows+=row('BUY_YES (48h)',h['BUY_YES cards (48h)'],'info');
  engineRows+=row('BUY_NO (48h)',h['BUY_NO cards (48h)'],'info');
  engineRows+=row('WATCH (48h)',h['WATCH cards (48h)'],'info');

  // ── Database ───────────────────────────────────────────────────────────────
  let dbRows='';
  dbRows+=row('File size',h['DB: size']||'—','info');
  const walV=h['DB: WAL']||'none';
  dbRows+=row('WAL file',walV,walV==='none'?'ok':'warn');
  dbRows+=row('SHM file',h['DB: SHM']||'none',h['DB: SHM']==='present'?'warn':'ok');

  // ── Data & Events ──────────────────────────────────────────────────────────
  const latestTs=h['Latest transcript'];
  let dataRows='';
  dataRows+=row('Total transcripts',h['Total transcripts'],h['Total transcripts']>0?'ok':'warn');
  dataRows+=row('Latest transcript',latestTs,ageStatus(latestTs));
  dataRows+=row('Phrase hits',h['Phrase hits'],h['Phrase hits']>0?'ok':'warn');
  dataRows+=row('Scheduled events',h['Scheduled events'],h['Scheduled events']>0?'ok':'warn');
  dataRows+=row('Live events now',h['Live events now'],h['Live events now']>0?'ok':'info');
  dataRows+=row('Outcome rows',h['Outcome review rows'],h['Outcome review rows']>0?'ok':'info');
  dataRows+=row('Realized P&amp;L',`$${Number(h['Realized PnL (all)']||0).toFixed(2)}`,h['Realized PnL (all)']>=0?'ok':'warn');
  if(h['Legacy model bets']) dataRows+=row('Legacy model',h['Legacy model bets'],'info');
  if(h['Bayesian model bets']) dataRows+=row('Bayesian model',h['Bayesian model bets'],'ok');

  // ── Signal File Freshness ──────────────────────────────────────────────────
  let sigRows='';
  const SIG_FILES=[
    ['signal_context','Signal Context',30,120],
    ['wh_schedule','WH Schedule',120,480],
    ['polymarket','Polymarket Prices',30,120],
    ['truth_social','Truth Social Posts',120,720],
    ['bias_map','Bias Map',360,1440],
    ['rolling_rates','Rolling Hit Rates',360,1440],
    ['hazard_rates','Hazard Rates',360,1440],
  ];
  for(const [key,label,warnM,errM] of SIG_FILES){
    const m=h[`Files: ${key}`];
    sigRows+=row(label,fmtAge(m),ageMinStatus(m,warnM,errM));
  }
  if(h['TS: post count']!=null)sigRows+=row('TS posts loaded',h['TS: post count'],'info');
  if(h['TS: newest post'])sigRows+=row('TS newest post',h['TS: newest post'],ageMinStatus(
    parseFloat(h['TS: newest post']),120,720));

  // ── Calibration ────────────────────────────────────────────────────────────
  const calibKeys=Object.keys(h).filter(k=>k.startsWith('Calib:'));
  let calibRows='';
  const calibOk=h['Calib: params']&&h['Calib: params']!=='not fitted';
  for(const k of calibKeys){calibRows+=row(k.replace(/^Calib: /,''),h[k],calibOk?'ok':'err');}

  // ── Model Performance ──────────────────────────────────────────────────────
    function bssStatus(s){
      if(!s||s==='N/A')return 'info';
    if(s.includes('✅')||s.includes('✓'))return 'ok';
      if(s.includes('❌'))return 'err';
      return 'warn';
    }
  const perfKeys=Object.keys(h).filter(k=>k.startsWith('Perf:'));
  let perfRows='';
  for(const k of perfKeys){const v=String(h[k]);perfRows+=row(k.replace(/^Perf: /,''),v,bssStatus(v));}

  // ── Threshold Optimization ─────────────────────────────────────────────────
  const threshKeys=Object.keys(h).filter(k=>k.startsWith('Thresh:'));
  let threshRows='';
  for(const k of threshKeys){threshRows+=row(k.replace(/^Thresh: /,''),h[k],'info');}

  // ── Regime Alerts ──────────────────────────────────────────────────────────
  const totalAlerts=h['Regime: total alerts'];
  const highSev=h['Regime: high severity']||0;
  let regimeRows='';
  if(totalAlerts!=null){
    regimeRows+=row('Total alerts',totalAlerts,totalAlerts===0?'ok':highSev>0?'err':'warn');
    regimeRows+=row('High severity',highSev,highSev===0?'ok':'err');
    const rawAlerts=h['Regime: alerts_raw']||[];
    for(const a of rawAlerts){
      const codes=(a.codes||[a.speaker||'?']).join('+');
      const sev=a.severity||'low';
      const badgeCls=sev==='high'?'sys-badge-high':sev==='medium'?'sys-badge-med':'sys-badge-low';
      regimeRows+=`<div class="sys-alert-row">
        <span class="sys-badge ${badgeCls}">${sev}</span>
        <span style="flex:1;font-size:11px;color:var(--text2)">${codes} ${a.side||''}</span>
        <span style="font-size:11px;color:var(--text3);font-family:'JetBrains Mono',monospace">WR ${a.win_pct||0}% · ${a.n_bets||0}b · $${(a.total_pnl||0).toFixed(2)}</span>
      </div>`;
    }
  }

  // ── X / Twitter ────────────────────────────────────────────────────────────
  const xKeys=Object.keys(h).filter(k=>k.startsWith('X '));
  let xRows='';
  for(const k of xKeys){xRows+=row(k.replace(/^X /,''),h[k],'info');}

  // ── Assemble layout (2-column grid) ───────────────────────────────────────
  let html='<div class="sys-page">';
  html+=section('Engine Status','If offline → run <b>Start Engine</b> from Scripts tab',engineRows);
  html+=section('Database','WAL/SHM files present = active writes. Normal during engine run.',dbRows);
  html+=section('Data &amp; Events','Stale transcripts → <b>Ingest Corpus</b> · Stale outcomes → <b>Fetch Outcomes</b>',dataRows);
  html+=section('Signal File Freshness','Run <b>Fetch Signals</b> + <b>WH Schedule</b> + <b>Fetch Polymarket</b> to refresh',sigRows);
  if(calibRows) html+=section('Platt Calibration','Not fitted → run <b>Calibrate Base Rates</b>',calibRows);
  if(threshRows) html+=section('Threshold Optimization','Run <b>10. Optimize Thresholds</b> → <b>11. Tune Policy</b> to apply',threshRows);
  if(perfRows)   html+=section('Model Performance (BSS)','BSS &gt; 0 = model beats market mid · Run <b>Historical Backtest --save</b> to refresh',perfRows);
  if(regimeRows) html+=section('Regime Alerts','Losing signal patterns from <b>Detect Regimes</b>. High-severity combos are auto-blocked.',regimeRows);
  if(xRows)      html+=section('X / Twitter API','',xRows);
  html+='</div>';
  return html;
}

/* ─── Scripts tab ─── */
const SCRIPTS=[
  {id:'event_certainties', script:'scripts/extract_event_certainties.py', label:'Event Certainties',  desc:'Parse event title (bill names, people, countries) → p_overrides. Highest-value pre-event signal. Run first.', group:'Pre-Event',       tag:'preevent'},
  {id:'ts_phrases',        script:'scripts/extract_ts_phrases.py',        label:'Truth Social Floors', desc:'Match Trump\'s recent posts against active phrases → p_floors. Run after Event Certainties.',               group:'Pre-Event',       tag:'preevent'},
  {id:'nba_schedule',      script:'scripts/fetch_nba_schedule.py',        label:'NBA Schedule',        desc:'Fetch today\'s NBA game schedule and seed events into the DB. Run before NBA Certainties.',                  group:'Sports',          tag:'nba'},
  {id:'nba_certainties',   script:'scripts/extract_nba_certainties.py',   label:'NBA Certainties',     desc:'Inject arena/sponsor p_overrides (~92%) and universal phrase p_floors for all NBA games.',                  group:'Sports',          tag:'nba'},
  {id:'mlb_certainties',   script:'scripts/extract_mlb_certainties.py',   label:'MLB Certainties',     desc:'Inject ballpark p_overrides (~90%) and universal phrase floors for active MLB games. Run after Fetch Markets.',group:'Sports',         tag:'nba'},
  {id:'ncaab_certainties', script:'scripts/extract_ncaab_certainties.py', label:'NCAAB Certainties',   desc:'Inject empirical phrase floors for active NCAAB mention markets based on resolved outcomes.',               group:'Sports',          tag:'nba'},
  {id:'fetch_markets',     script:'scripts/fetch_markets.py',             label:'Fetch Markets',       desc:'Pull latest mention markets from the Kalshi API and update local cache',                                    group:'Data Fetching',   tag:'data'},
  {id:'fetch_outcomes',    script:'scripts/fetch_outcomes.py',            label:'Fetch Outcomes',      desc:'Download finalized market outcomes from Kalshi for calibration',                                            group:'Data Fetching',   tag:'data'},
  {id:'fetch_polymarket',  script:'scripts/fetch_polymarket.py',          label:'Fetch Polymarket',    desc:'Sync Polymarket prices and match to Kalshi mention markets',                                                group:'Data Fetching',   tag:'data'},
  {id:'fetch_wh',          script:'scripts/fetch_wh_schedule.py',        label:'WH Schedule',         desc:'Fetch the White House daily schedule for event context and planning',                                       group:'Data Fetching',   tag:'data'},
  {id:'fetch_signals',     script:'scripts/fetch_signals.py',             label:'Fetch Signals',       desc:'Aggregate signal data (news, buzz, X) for the LLM reasoning pass',                                        group:'Data Fetching',   tag:'data'},
  {id:'fetch_news',        script:'scripts/fetch_news_signals.py',        label:'Fetch News',          desc:'Pull latest news signals for phrase relevance scoring',                                                    group:'Data Fetching',   tag:'data'},
  {id:'fetch_x',           script:'scripts/fetch_x_signals.py',          label:'Fetch X Signals',     desc:'Collect X/Twitter watchlist signals for monitored accounts',                                               group:'Data Fetching',   tag:'data'},
  {id:'fetch_wallet',      script:'scripts/fetch_wallet_flow.py',         label:'Wallet Flow',         desc:'Build alpha signals from public Polymarket wallet trade activity',                                         group:'Data Fetching',   tag:'data'},
  {id:'fetch_settlements', script:'scripts/fetch_settlements.py',         label:'Fetch Settlements',   desc:'Pull recently settled Kalshi markets for same-event boosting signals',                                    group:'Data Fetching',   tag:'data'},
  {id:'fetch_hot',         script:'scripts/fetch_hot_events.py',          label:'Hot Events',          desc:'Fast-fetch same-day specific-event markets for rapid response',                                           group:'Data Fetching',   tag:'data'},
  {id:'fetch_fed',         script:'scripts/fetch_fed_transcripts.py',     label:'Fed Transcripts',     desc:'Scrape latest Fed/Powell speech transcripts into the corpus for Powell market scoring.',                   group:'Data Fetching',   tag:'data'},
  {id:'analyze_event',     script:'scripts/analyze_event.py',             label:'Analyze Event Signals',desc:'Generate per-event LLM phrase multipliers for all active events',                                        group:'AI Intelligence', tag:'llm'},
  {id:'analyze_signals',   script:'scripts/analyze_signals.py',           label:'Analyze Global Signals',desc:'Run the LLM reasoning layer to score phrase probability adjustments',                                   group:'AI Intelligence', tag:'llm'},
  {id:'backtest_outcomes', script:'scripts/backtest_outcomes.py',         label:'Historical Backtest', desc:'Backtest the scoring model against all resolved Kalshi mention-market outcomes',                          group:'Backtesting',     tag:'backtest'},
  {id:'backtest_scorecards',script:'scripts/backtest_scorecards.py',      label:'Scorecards Backtest', desc:'Simulate model performance on recent live action-card scorecards',                                        group:'Backtesting',     tag:'backtest'},
  {id:'backtest_llm',      script:'scripts/backtest_llm_impact.py',       label:'LLM Impact Analysis', desc:'Quantify the LLM layer\'s contribution to historical prediction accuracy',                               group:'Backtesting',     tag:'backtest'},
  {id:'rolling_rates',     script:'scripts/compute_rolling_rates.py',     label:'1. Rolling Rates',    desc:'Compute recency-weighted phrase hit rates from resolved outcomes',                                        group:'Calibration',     tag:'calib'},
  {id:'cooccurrence',      script:'scripts/compute_cooccurrence.py',      label:'2. Co-occurrence',    desc:'Build phrase co-occurrence index from historical outcome data',                                           group:'Calibration',     tag:'calib'},
  {id:'compute_base',      script:'scripts/compute_base_rates.py',        label:'3. Compute Base Rates',desc:'Derive per-phrase base rates from resolved outcomes. Must run before Calibrate Base Rates.',             group:'Calibration',     tag:'calib'},
  {id:'bias_map',          script:'scripts/compute_bias_map.py',          label:'4. Compute Bias Map', desc:'Build BIAS_MAP_OVERPRICED/UNDERPRICED signals from systematic market mispricing patterns.',              group:'Calibration',     tag:'calib'},
  {id:'hazard_rates',      script:'scripts/compute_hazard_rates.py',      label:'5. Hazard Rates',     desc:'Derive phrase-specific time-decay survival curves from corpus (replaces static decay).',                 group:'Calibration',     tag:'calib'},
  {id:'phrase_trends',     script:'scripts/compute_phrase_trends.py',     label:'6. Phrase Trends',    desc:'Compute 30d vs 90d YES-rate trends — flags VOCAB_TRENDING_UP/DOWN signals',                            group:'Calibration',     tag:'calib'},
  {id:'calibrate_base',    script:'scripts/calibrate_base_rates.py',      label:'7. Calibrate Base Rates',desc:'Refit Platt scaling parameters from resolved outcomes. Run after Compute Base Rates.',               group:'Calibration',     tag:'calib'},
  {id:'price_velocity',    script:'scripts/compute_price_velocity.py',    label:'8. Price Velocity',   desc:'Calculate momentum signals across active Kalshi markets',                                                group:'Calibration',     tag:'calib'},
  {id:'detect_regimes',    script:'scripts/detect_regimes.py',            label:'9. Detect Regimes',   desc:'Scan outcomes for systematically losing signal patterns; writes regime_alerts.json.',                    group:'Calibration',     tag:'calib'},
  {id:'optimize_thresh',   script:'scripts/optimize_thresholds.py',       label:'10. Optimize Thresholds',desc:'Grid search EV/Kelly/confidence thresholds on historical outcomes; writes threshold_optimization.json.',group:'Calibration',  tag:'calib'},
  {id:'tune_policy',       script:'scripts/tune_policy.py',               label:'11. Tune Policy',     desc:'Apply optimized thresholds — run last after all calibration steps complete.',                            group:'Calibration',     tag:'calib'},
  {id:'health_check',      script:'scripts/health_check.py',              label:'Health Check',        desc:'Run event-day freshness, coverage, and signal completeness checks',                                      group:'Health & Reporting',tag:'health'},
  {id:'report_outcomes',   script:'scripts/report_outcomes.py',           label:'Report Outcomes',     desc:'Generate a performance summary of recent betting outcomes and P&L',                                      group:'Health & Reporting',tag:'health'},
  {id:'post_event',        script:'scripts/post_event_summary.py',        label:'Post-Event Summary',  desc:'Automated postmortem and drift detector for completed events',                                           group:'Health & Reporting',tag:'health'},
  {id:'record_outcomes',   script:'scripts/record_outcomes.py',           label:'Record Outcomes',     desc:'Record bet outcomes into the database for calibration tracking',                                         group:'Health & Reporting',tag:'health'},
  {id:'prune_snapshots',   script:'scripts/prune_snapshots.py',           label:'Prune Snapshots',     desc:'Clean up stale market snapshots and rotate JSONL log files',                                             group:'Health & Reporting',tag:'health'},
  {id:'archive_bets',      script:'scripts/archive_bet_decisions.py',     label:'Archive Bet Decisions',desc:'Archive resolved action cards and bet decisions to long-term storage',                                  group:'Health & Reporting',tag:'health'},
  {id:'audit_series',      script:'scripts/audit_mention_series.py',      label:'Audit Mention Series',desc:'Compare fetch_markets vs fetch_outcomes series coverage; flags missing or mismatched tickers.',          group:'Health & Reporting',tag:'health'},
  {id:'ingest_corpus',     script:'scripts/ingest_corpus.py',             label:'Ingest Corpus',       desc:'Bulk-load transcript files from data/corpus/ into the SQLite database',                                  group:'Corpus',          tag:'corpus'},
  {id:'analyze_corpus',    script:'scripts/analyze_corpus.py',            label:'Analyze Corpus',      desc:'Analyze scraped transcript data and calibrate base phrase rates',                                        group:'Corpus',          tag:'corpus'},
  {id:'process_truth',     script:'scripts/process_truth_social.py',      label:'Process Truth Social',desc:'Process Trump Truth Social posts and update phrase signal boosts',                                       group:'Corpus',          tag:'corpus'},
  {id:'scrape_corpus',     script:'scripts/scrape_corpus.py',             label:'Scrape Corpus',       desc:'Scrape historical transcripts into the local corpus via Browser Relay',                                  group:'Corpus',          tag:'corpus'},
  {id:'start_engine',      script:'scripts/start_engine.py',              label:'Start Engine',        desc:'Launch the scoring engine as a background daemon. No-op if already running.',                            group:'Engine Control',  tag:'engine'},
  {id:'stop_engine',       script:'scripts/stop_engine.py',               label:'Stop Engine',         desc:'Gracefully stop the runner, watcher, ingestor, and scorer (SIGTERM → SIGKILL). Dashboard keeps running.',group:'Engine Control',  tag:'danger'},
];
const SC_GROUP_ORDER=['Engine Control','Pre-Event','Sports','Data Fetching','AI Intelligence','Calibration','Backtesting','Health & Reporting','Corpus'];
let scJobs={};     // { [jobId]: {id,script,label,status,output,startedAt,endedAt} }
let scTimers={};   // { [jobId]: intervalId }
const SC_POLL_MS=900;
const SC_MAX_RUN_SEC=600; // 10 min hard timeout

function renderScripts(){
  const tagColors={preevent:'t-preevent',nba:'t-nba',llm:'t-llm',data:'t-data',backtest:'t-backtest',calib:'t-calib',health:'t-health',corpus:'t-corpus',danger:'t-danger',engine:'t-engine'};
  const groupOrder=SC_GROUP_ORDER;
  const groups=groupOrder.filter(g=>SCRIPTS.some(s=>s.group===g));
  let html='<div class="scripts-page">';
  for(const g of groups){
    const items=SCRIPTS.filter(s=>s.group===g);
    const isDanger=g==='Engine Control';
    const isPreEvent=g==='Pre-Event';
    const isSports=g==='Sports';
    html+=`<div class="sg-block${isDanger?' sg-danger':isPreEvent?' sg-preevent':isSports?' sg-sports':''}">`;
    html+=`<div class="sg-head"><span class="sg-label">${g}</span><span class="sg-line"></span></div>`;
    html+=`<div class="sg-cards">`;
    for(const s of items){
      const job=Object.values(scJobs).filter(j=>j.id===s.id).sort((a,b)=>b.startedAt-a.startedAt)[0];
      const st=job?job.status:'idle';
      const cardCls=st==='running'?'sc-running':st==='done'?'sc-done':st==='error'?'sc-error':'';
      const btnTxt=st==='running'?'Running...':st==='done'?'Run Again':st==='error'?'Retry':'Run';
      const btnCls=st==='running'?'btn-running':st==='done'?'btn-done':st==='error'?'btn-error':'';
      const btnDis=st==='running'?'disabled':'';
      const tc=tagColors[s.tag]||'';
      html+=`<div class="sc-card ${cardCls}" id="sc-card-${s.id}">`;
      html+=`<div class="sc-top">`;
      html+=`<div class="sc-info"><div class="sc-name">${s.label}</div><div class="sc-desc">${s.desc}</div>`;
      html+=`<div class="sc-meta"><span class="sc-tag ${tc}">${s.tag}</span><span class="sc-file">${s.script.split('/').pop()}</span></div>`;
      html+=`</div>`;
      html+=`<button class="sc-btn ${btnCls}" ${btnDis} onclick="scRun('${s.id}','${s.script}')" id="sc-btn-${s.id}">${btnTxt}</button>`;
      html+=`</div>`;
      if(job&&job.jobId){html+=scOutHTML(job);}
      html+=`</div>`;
    }
    html+='</div></div>';
  }
  html+='</div>';
  return html;
}

function scOutHTML(job){
  const vis=job.status!=='idle'?'visible':'';
  const dc=job.status==='running'?'d-running':job.status==='done'?'d-done':'d-error';
  const statusTxt=job.status==='running'?'Running':'done'===job.status?'Completed':'Failed';
  const elapsed=job.endedAt?(((job.endedAt-job.startedAt)/1000).toFixed(1)+'s'):(job.startedAt?elapsedSince(job.startedAt):'');
  const lines=(job.output||[]).slice(-200).map(l=>{
    const esc=l.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    if(/error|exception|traceback|failed/i.test(l))return`<span class="t-err">${esc}</span>`;
    if(/warning|warn/i.test(l))return`<span class="t-warn">${esc}</span>`;
    if(/done|success|complete|finished/i.test(l))return`<span class="t-ok">${esc}</span>`;
    return esc;
  }).join('\n');
  return `<div class="sc-out ${vis}" id="sc-out-${job.jobId}">
    <div class="sc-status-bar"><span class="sc-dot ${dc}"></span><span>${statusTxt}</span><span class="sc-elapsed" id="sc-el-${job.jobId}">${elapsed}</span></div>
    <div class="sc-term" id="sc-term-${job.jobId}">${lines||'<span style="opacity:.4">Waiting for output...</span>'}</div>
  </div>`;
}

function elapsedSince(ts){const s=Math.round((Date.now()/1000)-ts);return s<60?s+'s':Math.round(s/60)+'m';}

function scToast(msg,type=''){
  const el=document.createElement('div');
  el.className='sc-toast'+(type?' '+type:'');
  el.textContent=msg;
  document.body.appendChild(el);
  setTimeout(()=>{el.style.opacity='0';setTimeout(()=>el.remove(),400)},3200);
}

async function scRun(id, script){
  const btn=document.getElementById('sc-btn-'+id);
  if(!btn||btn.disabled)return;

  // Guard: prevent concurrent data-fetch scripts to avoid Kalshi API rate limits
  const s=SCRIPTS.find(x=>x.id===id);
  if(s&&s.tag==='data'){
    const running=Object.values(scJobs).filter(j=>j.status==='running');
    const dataRunning=running.some(j=>{const sc=SCRIPTS.find(x=>x.id===j.id);return sc&&sc.tag==='data';});
    if(dataRunning){
      scToast('A data-fetch script is already running — wait for it to finish to avoid Kalshi rate limits','warn');
      return;
    }
  }

  btn.disabled=true;btn.textContent='Starting...';btn.className='sc-btn btn-running';
  const card=document.getElementById('sc-card-'+id);
  const isDanger=s&&s.tag==='danger';
  if(card)card.className='sc-card sc-running';
  try{
    const r=await fetch('/api/run-script',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({script,id})});
    if(!r.ok){const t=await r.text();throw new Error(t||r.statusText);}
    const data=await r.json();
    if(!data.job_id)throw new Error(data.error||'no job_id returned');
    const job={jobId:data.job_id,id,script,label:s?s.label:id,
      status:'running',output:[],startedAt:Date.now()/1000,endedAt:null};
    scJobs[data.job_id]=job;
    btn.textContent='Running...';
    // Inject output panel beneath the card header
    if(!document.getElementById('sc-out-'+data.job_id)){
      const top=card?card.querySelector('.sc-top'):null;
      if(top)top.insertAdjacentHTML('afterend',scOutHTML(job));
    }
    // Start polling; also set a hard client-side timeout
    scTimers[data.job_id]=setInterval(()=>scPoll(data.job_id,id),SC_POLL_MS);
    setTimeout(()=>scForceTimeout(data.job_id,id),SC_MAX_RUN_SEC*1000);
  }catch(e){
    btn.disabled=false;btn.textContent='Error — Retry';btn.className='sc-btn btn-error';
    if(card)card.className='sc-card sc-error';
    scToast('Failed to start script: '+e.message,'err');
  }
}

function scForceTimeout(jobId,id){
  const job=scJobs[jobId];
  if(!job||job.status!=='running')return;
  clearInterval(scTimers[jobId]);delete scTimers[jobId];
  job.status='error';job.endedAt=Date.now()/1000;
  job.output.push('[TIMEOUT] Script exceeded 10 minute limit and was marked as timed out.');
  scUpdateUI(jobId,id,'error');
  scToast('Script timed out after 10 minutes','err');
}

async function scPoll(jobId, id){
  const job=scJobs[jobId];
  if(!job)return;
  try{
    const r=await fetch('/api/script-output?job='+encodeURIComponent(jobId)+'&t='+Date.now());
    // If backend restarted the job is gone — stop polling and mark error
    if(r.status===404){
      clearInterval(scTimers[jobId]);delete scTimers[jobId];
      job.status='error';job.endedAt=Date.now()/1000;
      job.output.push('[LOST] Backend restarted — job state no longer available.');
      scUpdateUI(jobId,id,'error');
      scToast('Script job was lost (backend restarted)','err');
      return;
    }
    if(!r.ok)throw new Error('HTTP '+r.status);
    const data=await r.json();
    job.status=data.status;job.output=data.output||[];
    if(data.status!=='running'){
      job.endedAt=Date.now()/1000;
      clearInterval(scTimers[jobId]);delete scTimers[jobId];
    }
    scUpdateUI(jobId,id,data.status);
  }catch(e){
    // Network error — stop polling to prevent infinite retry loops
    job._pollErrors=(job._pollErrors||0)+1;
    if(job._pollErrors>=5){
      clearInterval(scTimers[jobId]);delete scTimers[jobId];
      job.status='error';job.endedAt=Date.now()/1000;
      job.output.push('[POLL ERROR] Lost connection to dashboard. Script may still be running.');
      scUpdateUI(jobId,id,'error');
    }
  }
}

function scUpdateUI(jobId,id,status){
  const job=scJobs[jobId];if(!job)return;
  const term=document.getElementById('sc-term-'+jobId);
  const outEl=document.getElementById('sc-out-'+jobId);
  const elEl=document.getElementById('sc-el-'+jobId);
  const card=document.getElementById('sc-card-'+id);
  const btn=document.getElementById('sc-btn-'+id);
  const dot=outEl?outEl.querySelector('.sc-dot'):null;
  const statusSpan=outEl?outEl.querySelector('.sc-status-bar span:nth-child(2)'):null;
  if(term){
    const lines=(job.output||[]).slice(-200).map(l=>{
      const esc=l.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      if(/error|exception|traceback|failed/i.test(l))return`<span class="t-err">${esc}</span>`;
      if(/warning|warn/i.test(l))return`<span class="t-warn">${esc}</span>`;
      if(/done|success|complete|finished|stopped/i.test(l))return`<span class="t-ok">${esc}</span>`;
      return esc;
    }).join('\n');
    term.innerHTML=lines||'<span style="opacity:.4">Waiting for output...</span>';
    term.scrollTop=term.scrollHeight;
  }
  if(elEl){const sec=job.endedAt?((job.endedAt-job.startedAt).toFixed(1)+'s'):elapsedSince(job.startedAt);elEl.textContent=sec;}
  if(dot)dot.className='sc-dot '+(status==='running'?'d-running':status==='done'?'d-done':'d-error');
  if(statusSpan)statusSpan.textContent=status==='running'?'Running':status==='done'?'Completed':'Failed';
  if(outEl)outEl.classList.add('visible');
  if(status!=='running'){
    if(card)card.className='sc-card '+(status==='done'?'sc-done':'sc-error');
    if(btn){btn.disabled=false;btn.textContent=status==='done'?'Run Again':'Retry';
      btn.className='sc-btn '+(status==='done'?'btn-done':'btn-error');}
  }
}

function bindScripts(){}

function emp(t,d){return `<div class="empty"><h3>${t}</h3><p>${d}</p></div>`}

setAR();
doRefresh();
</script>
</body>
</html>"""


def _get_conn() -> sqlite3.Connection:
    return connect(DB_PATH)


def _load_event_meta() -> dict[str, dict]:
    """Load real Kalshi event titles and metadata from the markets cache."""
    from collections import Counter

    meta: dict[str, dict] = {}
    if not KALSHI_CACHE.exists():
        return meta

    data = json.loads(KALSHI_CACHE.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)

    by_event: dict[str, list[dict]] = {}
    for m in data.get("markets", []):
        et = m.get("event_ticker", "")
        by_event.setdefault(et, []).append(m)

    for et, mkts in by_event.items():
        first = mkts[0]
        close_str = first.get("close_time", "")
        speaker = first.get("speaker", "")

        closes_in = ""
        is_closed = False
        if close_str:
            try:
                ct = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                if ct.tzinfo is None:
                    ct = ct.replace(tzinfo=timezone.utc)
                delta = ct - now
                total_sec = delta.total_seconds()
                days = delta.days
                hours = delta.seconds // 3600
                if total_sec < 0:
                    closes_in = "ended"
                    is_closed = True
                elif total_sec < 3600:
                    mins = int(total_sec / 60)
                    closes_in = f"in {mins}m"
                elif days == 0:
                    closes_in = f"in {hours}h"
                elif days == 1:
                    closes_in = "tomorrow"
                elif days < 7:
                    closes_in = f"in {days}d"
                else:
                    closes_in = ct.strftime("%b %d")
            except Exception:
                pass

        titles = [m.get("title", "") for m in mkts]
        counts = Counter(titles)
        most_common, mc_count = counts.most_common(1)[0]

        if mc_count >= len(titles) * 0.5 and "what will" in most_common.lower():
            label = most_common
        else:
            sample = titles[0] if titles else ""
            if sample:
                lower = sample.lower()
                marker = " say "
                if lower.startswith("will ") and marker in lower:
                    say_idx = lower.find(marker)
                    speaker_part = sample[len("Will "):say_idx].strip()
                    tail = sample[say_idx + len(marker):]
                    tail_lower = tail.lower()
                    window_idx = -1
                    for wm in (" before ", " during ", " at "):
                        pos = tail_lower.find(wm)
                        if pos >= 0 and (window_idx < 0 or pos < window_idx):
                            window_idx = pos
                    if speaker_part and window_idx >= 0:
                        window_part = tail[window_idx + 1:].strip()
                        label = f"What will {speaker_part} say {window_part}"
                    else:
                        label = sample
                else:
                    label = sample
            else:
                label = et

        series = et.rsplit("-", 1)[0] if "-" in et else et
        if "nickname" in series.lower():
            label += " (Nicknames)"
        elif "saymonth" in series.lower() and "monthly" not in label.lower():
            label += " (Monthly)"

        label = _normalize_event_label(
            label,
            _fallback_label(et),
            speaker=speaker,
            event_ticker=et,
        )

        meta[et] = {
            "label": label,
            "speaker": speaker,
            "close_time": close_str,
            "closes_in": closes_in,
            "is_closed": is_closed,
        }
    return meta


def _query_status(conn: sqlite3.Connection, coverage: dict | None = None) -> dict:
    import datetime as _dt
    _h48 = (_dt.datetime.utcnow() - _dt.timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S")
    market_count = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    card_count = conn.execute(
        "SELECT COUNT(*) FROM action_cards WHERE ts >= ?", (_h48,)
    ).fetchone()[0]
    transcript_count = conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
    hit_count = conn.execute("SELECT COUNT(*) FROM phrase_hits").fetchone()[0]

    latest_snap = conn.execute(
        "SELECT MAX(ts) FROM market_snapshots WHERE ts >= ?", (_h48,)
    ).fetchone()[0] or ""
    snap_age = 9999.0
    if latest_snap:
        try:
            snap_dt = datetime.fromisoformat(latest_snap.replace("Z", "+00:00"))
            if snap_dt.tzinfo is None:
                snap_dt = snap_dt.replace(tzinfo=timezone.utc)
            snap_age = (datetime.now(timezone.utc) - snap_dt).total_seconds()
        except Exception:
            pass

    buy_yes = conn.execute(
        "SELECT COUNT(DISTINCT market_id) FROM action_cards WHERE side='BUY_YES' AND market_id LIKE 'KX%' AND ts >= ?",
        (_h48,)
    ).fetchone()[0]
    buy_no = conn.execute(
        "SELECT COUNT(DISTINCT market_id) FROM action_cards WHERE side='BUY_NO' AND market_id LIKE 'KX%' AND ts >= ?",
        (_h48,)
    ).fetchone()[0]

    poly_match = 0
    wallet_match = 0
    try:
        poly_match = conn.execute(
            """SELECT COUNT(DISTINCT market_id) FROM action_cards
               WHERE json_extract(raw_json, '$.poly_yes') IS NOT NULL
               AND side != 'WATCH' AND ts >= ?""",
            (_h48,)
        ).fetchone()[0]
        wallet_match = conn.execute(
            """SELECT COUNT(DISTINCT market_id) FROM action_cards
               WHERE json_extract(raw_json, '$.wallet_confidence') IS NOT NULL
               AND side != 'WATCH' AND ts >= ?""",
            (_h48,)
        ).fetchone()[0]
    except Exception:
        pass

    out = {
        "market_count": market_count,
        "card_count": card_count,
        "transcript_count": transcript_count,
        "phrase_hit_count": hit_count,
        "snapshot_age_sec": round(snap_age),
        "buy_yes_count": buy_yes,
        "buy_no_count": buy_no,
        "poly_match_count": poly_match,
        "wallet_signal_count": wallet_match,
    }
    if coverage:
        out["open_event_count"] = int(coverage.get("open_event_count", 0))
        out["tracked_event_count"] = int(coverage.get("tracked_event_count", 0))
        out["untracked_event_count"] = int(coverage.get("untracked_event_count", 0))
        out["untracked_market_count"] = int(coverage.get("untracked_market_count", 0))
    return out


def _load_market_cache() -> dict[str, dict]:
    """Load market metadata from kalshi_markets.json keyed by ticker."""
    if not KALSHI_CACHE.exists():
        return {}
    data = json.loads(KALSHI_CACHE.read_text(encoding="utf-8"))
    return {m["ticker"]: m for m in data.get("markets", [])}


def _load_events_cache() -> dict[str, dict]:
    """Load event-level discovery cache keyed by event_ticker."""
    if not EVENTS_CACHE.exists():
        return {}
    try:
        data = json.loads(EVENTS_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for e in data.get("events", []):
        if not isinstance(e, dict):
            continue
        et = str(e.get("event_ticker", "")).strip()
        if not et:
            continue
        out[et] = e
    return out


def _load_outcome_rates() -> dict[str, dict]:
    """Load historical YES/NO rates keyed by 'speaker:phrase_lower'."""
    from collections import defaultdict

    if not OUTCOMES_CACHE.exists():
        return {}
    data = json.loads(OUTCOMES_CACHE.read_text(encoding="utf-8"))
    stats: dict[str, dict] = defaultdict(lambda: {"yes": 0, "no": 0})
    for m in data.get("markets", []):
        result = m.get("result")
        if result not in ("yes", "no"):
            continue
        phrase = (m.get("primary_phrase") or "").lower()
        speaker = m.get("speaker", "")
        if not phrase or not speaker:
            continue
        stats[f"{speaker}:{phrase}"][result] += 1
    return dict(stats)


def _query_confirmed_phrases(conn: sqlite3.Connection) -> set[str]:
    """Return set of phrases confirmed via transcript hits today."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT DISTINCT lower(phrase) FROM phrase_hits WHERE hit_date = ?",
        (today,),
    ).fetchall()
    return {r[0] for r in rows}


def _query_all_cards(
    conn: sqlite3.Connection,
    market_cache: dict[str, dict],
    outcome_rates: dict[str, dict],
    confirmed_phrases: set[str],
    snapshot_market_meta: dict[str, dict] | None = None,
    *,
    market_id_like: str = "KX%",
    apply_blocked_filter: bool = True,
) -> list[dict]:
    import datetime as _dt
    _cutoff = (_dt.datetime.utcnow() - _dt.timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute("""
        WITH recent AS (
            SELECT market_id, MAX(id) AS max_id
            FROM action_cards
            WHERE market_id LIKE ?
              AND ts >= ?
            GROUP BY market_id
        )
        SELECT ac.market_id, ac.phrase, ac.side, ac.p_literal, ac.yes_ask, ac.no_ask,
               ac.ev_yes, ac.ev_no, ac.exec_price_hint, ac.spread_ok, ac.depth_ok, ac.gate_pass,
               ac.rationale, ac.raw_json
        FROM action_cards ac
        JOIN recent r ON ac.id = r.max_id
    """, (market_id_like, _cutoff)).fetchall()

    market_ids = [r["market_id"] for r in rows]
    latest_snapshot_meta = snapshot_market_meta or _load_latest_snapshot_market_meta(
        conn, market_ids=market_ids
    )

    # Authoritative subject from the markets table — takes priority over stale raw_json.
    # This ensures that DB fixes (e.g. correcting wrongly-inferred speaker=trump) are
    # reflected immediately without re-running the scorer.
    db_subjects: dict[str, str] = {}
    if market_ids:
        placeholders = ",".join("?" * len(market_ids))
        for row in conn.execute(
            f"SELECT market_id, subject FROM markets WHERE market_id IN ({placeholders})",
            market_ids,
        ).fetchall():
            if row["subject"] and row["subject"] != "auto":
                db_subjects[row["market_id"]] = row["subject"]

    cards = []
    for r in rows:
        mid = r["market_id"]
        cached_market = market_cache.get(mid, {})
        snap_market = latest_snapshot_meta.get(mid, {})

        # Skip sports, entertainment, and earnings markets (main Markets tab only).
        _series = cached_market.get("series_ticker", "") or snap_market.get("series_ticker", "")
        if apply_blocked_filter and _is_blocked_market(mid, _series):
            continue

        raw: dict = {}
        try:
            raw = json.loads(r["raw_json"])
        except Exception:
            pass

        phrase = r["phrase"]
        if not phrase:
            phrase = cached_market.get("primary_phrase", "")
        if not phrase:
            phrase = snap_market.get("primary_phrase", "")
        if not phrase:
            phrase = raw.get("phrase", "")
        phrase = (phrase or "").strip()
        if not phrase:
            # Last-resort readable fallback when upstream phrase is missing.
            maybe_slug = mid.rsplit("-", 1)[-1]
            if maybe_slug and maybe_slug != mid:
                phrase = maybe_slug.replace("_", " ").title()

        # Priority: DB markets.subject (authoritative) > kalshi_markets.json cache >
        #           snapshot title extraction > raw_json subject (stale, last resort).
        # raw_json is last because it may contain a stale speaker from before speaker
        # inference was fixed (e.g. subject=trump for non-trump speakers).
        speaker = (
            db_subjects.get(mid)
            or cached_market.get("speaker")
            or snap_market.get("speaker", "")
            or raw.get("subject", "")
        )
        event_ticker = cached_market.get("event_ticker", "")
        if not event_ticker:
            event_ticker = snap_market.get("event_ticker", "")
        if not event_ticker:
            parts = mid.split("-")
            if len(parts) >= 2:
                event_ticker = "-".join(parts[:2])
        if not event_ticker:
            ev = raw.get("event", {})
            if isinstance(ev, dict):
                ev_id = str(ev.get("event_id", ""))
                if ev_id:
                    event_ticker = ev_id.rsplit(":", 1)[-1]

        phrase_lower = phrase.lower()

        hit_confirmed = phrase_lower in confirmed_phrases

        hist_key = f"{speaker}:{phrase_lower}"
        hist = outcome_rates.get(hist_key)
        hist_yes = hist["yes"] if hist else None
        hist_total = (hist["yes"] + hist["no"]) if hist else None

        reasons_str = ",".join(raw.get("reason_codes", []))
        scores = raw.get("scores", {})
        cards.append({
            "market_id": mid,
            "phrase": phrase,
            "speaker": speaker,
            "event_ticker": event_ticker,
            "event_title": snap_market.get("title", ""),
            "close_time": snap_market.get("close_time", ""),
            "side": r["side"],
            "p_literal": r["p_literal"],
            # Calibration fields (present in new rows; None for old rows)
            "p_calibrated": raw.get("p_calibrated"),
            "calib_shift": raw.get("calib_shift"),
            "kl_divergence": raw.get("kl_divergence"),
            "kelly_fraction": raw.get("kelly_fraction"),
            "yes_ask": r["yes_ask"],
            "no_ask": r["no_ask"],
            "ev_yes": r["ev_yes"],
            "ev_no": r["ev_no"],
            "exec_price_hint": r["exec_price_hint"],
            "size_rec": raw.get("size_rec"),
            "gate_pass": r["gate_pass"],
            "reasons": reasons_str,
            "poly_yes": raw.get("poly_yes"),
            "poly_confidence": raw.get("poly_confidence"),
            "poly_strong_match": raw.get("poly_strong_match", False),
            "wallet_confidence": raw.get("wallet_confidence"),
            "wallet_bias": raw.get("wallet_bias"),
            "score_confidence": raw.get("score_confidence"),
            "risk_adjusted_ev_chosen": raw.get("risk_adjusted_ev_chosen"),
            "hit_confirmed": hit_confirmed,
            "hist_yes": hist_yes,
            "hist_total": hist_total,
            "llm_boost": scores.get("llm_boost"),
            "llm_boost_stated": scores.get("llm_boost_stated"),
            "llm_confidence": scores.get("llm_confidence", ""),
            "llm_direct_ev": scores.get("llm_direct_ev", False),
            "llm_reasoning": scores.get("llm_reasoning", ""),
            "llm_evidence": scores.get("llm_evidence", ""),
            "llm_topic": scores.get("llm_topic", ""),
        })
    return cards


def _load_latest_snapshot_market_meta(
    conn: sqlite3.Connection, market_ids: list[str] | None = None
) -> dict[str, dict]:
    """Best-effort metadata from latest market snapshot per market_id."""
    if not market_ids:
        return {}

    rows: list[sqlite3.Row] = []
    uniq_ids = sorted(set(market_ids))
    chunk_size = 400
    for i in range(0, len(uniq_ids), chunk_size):
        chunk = uniq_ids[i:i + chunk_size]
        placeholders = ",".join("?" for _ in chunk)
        import datetime as _dt
        _snap_cutoff = (_dt.datetime.utcnow() - _dt.timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S")
        # Bound by recent rows first so we only scan the last 48h of data.
        q = f"""
            SELECT s.market_id, s.raw_json
            FROM market_snapshots s
            JOIN (
                SELECT market_id, MAX(id) AS max_id
                FROM market_snapshots
                WHERE market_id IN ({placeholders})
                  AND ts >= '{_snap_cutoff}'
                GROUP BY market_id
            ) latest ON latest.max_id = s.id
        """
        rows.extend(conn.execute(q, tuple(chunk)).fetchall())

    meta: dict[str, dict] = {}
    for r in rows:
        market_id = r["market_id"]
        try:
            outer = json.loads(r["raw_json"] or "{}")
        except Exception:
            continue
        api = outer.get("raw_api")
        if not isinstance(api, dict):
            continue

        title = str(api.get("title") or "").strip()
        phrase = str(api.get("yes_sub_title") or api.get("no_sub_title") or "").strip()
        event_ticker = str(api.get("event_ticker") or "").strip()
        close_time = str(api.get("close_time") or "").strip()

        speaker = ""
        if title.lower().startswith("what will "):
            marker = " say "
            idx = title.lower().find(marker)
            if idx > len("what will "):
                speaker = title[len("what will "):idx].strip().lower()

        meta[market_id] = {
            "title": title,
            "primary_phrase": phrase,
            "event_ticker": event_ticker,
            "close_time": close_time,
            "speaker": speaker,
        }
    return meta


_SPEAKER_TITLE_STRIP = re.compile(
    r"^(Governor|Chairman|Secretary|Director|Senator|Representative|Rep\.|Dr\.|"
    r"President|Vice President|Ambassador|Administrator|Acting|Chief of Staff|"
    r"Attorney General)\s+",
    re.IGNORECASE,
)


def _clean_event_label(raw_title: str) -> str:
    """Convert raw Kalshi market title into a concise dashboard label.

    Handles the main patterns:
      • "What will X say during EVENT?"  → "X — EVENT"
      • "Will X say PHRASE before DATE?" → "X: PHRASE (before DATE)"
      • Redundant "What will X say during X At EVENT?" → "X — EVENT"
    """
    import re as _re

    t = raw_title.strip()

    # Pattern: "What will X say during EVENT?" (most political markets)
    m = _re.match(
        r"^What will (.+?) say during (.+?)\??$", t, _re.IGNORECASE
    )
    if m:
        speaker = m.group(1).strip()
        event = m.group(2).strip()
        # Remove redundant repetition: "Governor X say during Governor X At ..."
        # e.g. speaker="Governor Michael S. Barr", event="Governor Michael S. Barr At The Fed"
        last_name = speaker.split()[-1].lower() if speaker else ""
        if last_name and event.lower().startswith(speaker.lower()):
            # event starts with the full speaker name — strip it
            event = event[len(speaker):].strip().lstrip("@:,—-").strip()
        elif last_name and last_name in event.lower().split()[0].lower():
            # event starts with a title+last name variant — strip title prefix
            cleaned_speaker = _SPEAKER_TITLE_STRIP.sub("", speaker).strip()
            if event.lower().startswith(cleaned_speaker.lower()):
                event = event[len(cleaned_speaker):].strip().lstrip("@:,—-").strip()
        # Strip "at his/her/the" prefix from event context if it repeats press-conference info
        event = _re.sub(r"^at his\b", "", event, flags=_re.IGNORECASE).strip()
        # Use last name of speaker for brevity; keep full name for less-known figures
        short_name = speaker
        if _re.search(r"\b(Trump|Powell|Leavitt|Mamdani)\b",
                      speaker, _re.IGNORECASE):
            short_name = speaker.split()[-1]  # "Trump", "Powell", etc.
        else:
            # Strip honorific titles for display
            short_name = _SPEAKER_TITLE_STRIP.sub("", speaker).strip()
        return f"{short_name} — {event}" if event else short_name

    # Pattern: "Will X say PHRASE before/at DATE?"
    m2 = _re.match(
        r"^Will (.+?) say (.+?) (before|at|during) (.+?)\??$", t, _re.IGNORECASE
    )
    if m2:
        speaker = _SPEAKER_TITLE_STRIP.sub("", m2.group(1).strip()).strip()
        phrase = m2.group(2).strip().strip('"\'""')
        window = m2.group(4).strip().rstrip("?")
        return f'{speaker}: \u201c{phrase}\u201d by {window}'

    return t


def _merge_event_meta_from_cards(
    event_meta: dict[str, dict], cards: list[dict]
) -> dict[str, dict]:
    """Overlay cache labels with exact event titles found in snapshots."""
    from collections import Counter

    titles_by_event: dict[str, list[str]] = {}
    close_by_event: dict[str, list[str]] = {}
    speaker_by_event: dict[str, list[str]] = {}

    for c in cards:
        et = c.get("event_ticker", "")
        if not et:
            continue
        title = c.get("event_title", "")
        close_time = c.get("close_time", "")
        speaker = c.get("speaker", "")
        if title:
            titles_by_event.setdefault(et, []).append(title)
        if close_time:
            close_by_event.setdefault(et, []).append(close_time)
        if speaker:
            speaker_by_event.setdefault(et, []).append(speaker)

    now = datetime.now(timezone.utc)
    merged = dict(event_meta)
    for et, titles in titles_by_event.items():
        counts = Counter(titles)
        sample_title = counts.most_common(1)[0][0]
        existing_label = merged.get(et, {}).get("label", "").strip()

        # Always clean raw Kalshi title patterns, even if an existing_label is
        # stored — it may itself be a verbatim "What will X say during Y?" string.
        _raw_pattern = existing_label.lower().startswith("what will ") or (
            existing_label.lower().startswith("will ") and " say " in existing_label.lower()
        )
        if not existing_label or _raw_pattern:
            label = _clean_event_label(sample_title)
        else:
            label = existing_label

        close_str = ""
        closes_in = ""
        is_closed = False
        if close_by_event.get(et):
            close_str = Counter(close_by_event[et]).most_common(1)[0][0]
            try:
                ct = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                if ct.tzinfo is None:
                    ct = ct.replace(tzinfo=timezone.utc)
                delta = ct - now
                total_sec = delta.total_seconds()
                days = delta.days
                hours = delta.seconds // 3600
                if total_sec < 0:
                    closes_in = "ended"
                    is_closed = True
                elif total_sec < 3600:
                    mins = int(total_sec / 60)
                    closes_in = f"in {mins}m"
                elif days == 0:
                    closes_in = f"in {hours}h"
                elif days == 1:
                    closes_in = "tomorrow"
                elif days < 7:
                    closes_in = f"in {days}d"
                else:
                    closes_in = ct.strftime("%b %d")
            except Exception:
                pass

        speaker = merged.get(et, {}).get("speaker", "")
        if speaker_by_event.get(et):
            speaker = Counter(speaker_by_event[et]).most_common(1)[0][0]

        label = _normalize_event_label(
            label,
            _fallback_label(et),
            speaker=speaker,
            event_ticker=et,
        )

        merged[et] = {
            "label": label,
            "speaker": speaker,
            "close_time": close_str or merged.get(et, {}).get("close_time", ""),
            "closes_in": closes_in or merged.get(et, {}).get("closes_in", ""),
            "is_closed": is_closed if close_str else merged.get(et, {}).get("is_closed", False),
        }

    return merged


_DASHBOARD_SPEAKER_PATTERNS: list[tuple[str, list[str]]] = [
    ("leavitt", ["leavitt", "press secretary"]),
    ("mamdani", ["mamdani"]),
    ("fed", ["fomc", "federal reserve", "fed chair", "powell"]),
    ("nba", ["nba"]),
    ("ncaab", ["ncaa basketball", "ncaab", "march madness"]),
    ("mlb", ["mlb", "major league baseball"]),
    ("mma", ["mma", "ufc", "fight"]),
    ("trump", ["trump", "donald j. trump"]),
]


def _infer_speaker_from_text(text: str) -> str:
    low = (text or "").lower()
    for speaker, keywords in _DASHBOARD_SPEAKER_PATTERNS:
        if any(kw in low for kw in keywords):
            return speaker
    return ""


def _normalize_event_label(
    title: str,
    fallback: str,
    *,
    speaker: str = "",
    event_ticker: str = "",
) -> str:
    sample = (title or "").strip()
    if not sample:
        sample = fallback
    lower = sample.lower()
    marker = " say "
    out = sample
    if lower.startswith("will ") and marker in lower:
        say_idx = lower.find(marker)
        speaker_part = sample[len("Will "):say_idx].strip()
        tail = sample[say_idx + len(marker):]
        tail_low = tail.lower()
        window_idx = -1
        for wm in (" before ", " during ", " at "):
            pos = tail_low.find(wm)
            if pos >= 0 and (window_idx < 0 or pos < window_idx):
                window_idx = pos
        if speaker_part and window_idx >= 0:
            window_part = tail[window_idx + 1:].strip()
            out = f"What will {speaker_part} say {window_part}"

    # Prefer explicit person naming for readability in the dashboard.
    low_out = out.lower()
    if (
        speaker == "leavitt"
        or event_ticker.startswith("KXSECPRESSMENTION")
        or event_ticker.startswith("KXLEAVITT")
    ):
        if "white house press secretary" in low_out:
            out = (
                out.replace("the White House Press Secretary", "Karoline Leavitt")
                .replace("White House Press Secretary", "Karoline Leavitt")
                .replace("press secretary", "Karoline Leavitt")
            )
    return out


def _query_coverage(
    conn: sqlite3.Connection,
    market_cache: dict[str, dict],
    all_cards: list[dict],
) -> dict:
    """Build visibility diagnostics for open speaker events not in action cards."""
    card_by_market = {c["market_id"]: c for c in all_cards}
    filtered_status_market_count = 0
    filtered_unknown_speaker_market_count = 0
    import datetime as _dt
    _snap_cutoff = (_dt.datetime.utcnow() - _dt.timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute("""
        WITH latest AS (
            SELECT market_id, MAX(id) AS max_id
            FROM market_snapshots
            WHERE market_id LIKE 'KX%'
              AND ts >= ?
            GROUP BY market_id
        )
        SELECT s.market_id, s.raw_json, m.subject
        FROM latest l
        JOIN market_snapshots s ON s.id = l.max_id
        JOIN markets m ON m.market_id = s.market_id
    """, (_snap_cutoff,)).fetchall()

    events: dict[str, dict] = {}
    events_cache = _load_events_cache()
    for r in rows:
        market_id = r["market_id"]

        # Skip blocked series (sports, entertainment, earnings).
        cached_meta = market_cache.get(market_id, {})
        _st = cached_meta.get("series_ticker", "")
        if _is_blocked_market(market_id, _st):
            continue

        try:
            outer = json.loads(r["raw_json"] or "{}")
        except Exception:
            continue
        api = outer.get("raw_api")
        if not isinstance(api, dict):
            continue
        status = str(api.get("status", "")).strip().lower()
        if status not in {"open", "active"}:
            filtered_status_market_count += 1
            continue
        in_cache = market_id in market_cache

        title = str(api.get("title") or "").strip()
        rules = str(api.get("rules_primary") or "").strip()
        speaker = str(r["subject"] or "").strip().lower()
        speaker_resolved = bool(speaker and speaker not in ("other", "unknown", "auto", ""))
        if not speaker_resolved:
            inferred = _infer_speaker_from_text(f"{title} {rules}")
            speaker = inferred or "auto"
            speaker_resolved = bool(inferred)
        if not speaker_resolved:
            filtered_unknown_speaker_market_count += 1

        event_ticker = str(api.get("event_ticker") or "").strip()
        if not event_ticker:
            parts = market_id.split("-")
            event_ticker = "-".join(parts[:2]) if len(parts) >= 2 else market_id

        phrase = str(api.get("yes_sub_title") or api.get("no_sub_title") or "").strip()
        yes_ask_raw = api.get("yes_ask_dollars")
        no_ask_raw = api.get("no_ask_dollars")
        try:
            yes_ask = float(yes_ask_raw) if yes_ask_raw is not None else None
        except Exception:
            yes_ask = None
        try:
            no_ask = float(no_ask_raw) if no_ask_raw is not None else None
        except Exception:
            no_ask = None

        cached = market_cache.get(market_id, {})
        cache_has_phrase = bool(cached.get("primary_phrase") or cached.get("phrase_variants"))
        is_phrase_market = bool(cached.get("is_phrase_market", cache_has_phrase or phrase))
        has_card = market_id in card_by_market
        poly_present = bool(card_by_market.get(market_id, {}).get("poly_yes") is not None)
        wallet_present = bool(card_by_market.get(market_id, {}).get("wallet_confidence") is not None)

        reason_codes: list[str] = []
        if not has_card:
            reason_codes.append("NO_ACTION_CARD")
        if not in_cache:
            reason_codes.append("NOT_IN_CACHE")
        if not is_phrase_market:
            reason_codes.append("NON_PHRASE_MARKET")
        if has_card and not poly_present:
            reason_codes.append("NO_POLY_MATCH")
        if has_card and not wallet_present:
            reason_codes.append("NO_WALLET_SIGNAL")

        evt = events.setdefault(event_ticker, {
            "event_ticker": event_ticker,
            "label": _normalize_event_label(
                title,
                _fallback_label(event_ticker),
                speaker=speaker,
                event_ticker=event_ticker,
            ),
            "speaker": speaker,
            "has_scored": False,
            "first_seen_at": "",
            "open_markets": 0,
            "snapshot_markets": 0,
            "scored_markets": 0,
            "poly_markets": 0,
            "wallet_markets": 0,
            "not_in_cache_markets": 0,
            "non_phrase_markets": 0,
            "unresolved_speaker_markets": 0,
            "discovered_in_events_cache": False,
            "reason_codes": set(),
            "sample_market": {},
        })
        evt["open_markets"] += 1
        evt["snapshot_markets"] += 1
        if has_card:
            evt["scored_markets"] += 1
        if poly_present:
            evt["poly_markets"] += 1
        if wallet_present:
            evt["wallet_markets"] += 1
        if market_id not in market_cache:
            evt["not_in_cache_markets"] += 1
        if not is_phrase_market:
            evt["non_phrase_markets"] += 1
        if not speaker_resolved:
            evt["unresolved_speaker_markets"] += 1
        if not evt.get("sample_market"):
            evt["sample_market"] = {
                "market_id": market_id,
                "phrase": phrase,
                "yes_ask": yes_ask,
                "no_ask": no_ask,
            }

    # Include events discovered from /events endpoint even when no snapshots/cards exist.
    for et, e in events_cache.items():
        title = str(e.get("title", "")).strip()
        speaker = str(e.get("speaker", "")).strip().lower()
        speaker_resolved = True
        if not speaker or speaker in ("unknown", "other", "auto"):
            inferred = _infer_speaker_from_text(title)
            speaker = inferred or "auto"
            speaker_resolved = bool(inferred)
        open_markets = int(e.get("open_markets", 0) or 0)
        if not speaker_resolved and open_markets > 0:
            filtered_unknown_speaker_market_count += open_markets
        evt = events.setdefault(
            et,
            {
                "event_ticker": et,
                "label": _normalize_event_label(
                    title,
                    _fallback_label(et),
                    speaker=speaker,
                    event_ticker=et,
                ),
                "speaker": speaker,
                "has_scored": False,
                "first_seen_at": str(e.get("first_seen_at") or ""),
                "open_markets": open_markets,
                "snapshot_markets": 0,
                "scored_markets": 0,
                "poly_markets": 0,
                "wallet_markets": 0,
                "not_in_cache_markets": 0,
                "non_phrase_markets": 0,
                "unresolved_speaker_markets": open_markets if not speaker_resolved else 0,
                "discovered_in_events_cache": True,
                "reason_codes": set(),
                "sample_market": {},
            },
        )
        evt["discovered_in_events_cache"] = True
        if not evt.get("first_seen_at"):
            evt["first_seen_at"] = str(e.get("first_seen_at") or "")
        if open_markets > evt.get("open_markets", 0):
            evt["open_markets"] = open_markets
        if not speaker_resolved:
            evt["unresolved_speaker_markets"] = max(
                int(evt.get("unresolved_speaker_markets", 0)),
                open_markets,
            )
        if not evt.get("label") and title:
            evt["label"] = _normalize_event_label(
                title,
                _fallback_label(et),
                speaker=speaker,
                event_ticker=et,
            )

    out_events: list[dict] = []
    untracked_event_count = 0
    untracked_market_count = 0
    for event in events.values():
        open_m = int(event.get("open_markets", 0))
        snapshot_m = int(event.get("snapshot_markets", 0))
        scored_m = int(event.get("scored_markets", 0))
        poly_m = int(event.get("poly_markets", 0))
        wallet_m = int(event.get("wallet_markets", 0))
        miss_cache_m = int(event.get("not_in_cache_markets", 0))
        non_phrase_m = int(event.get("non_phrase_markets", 0))
        unresolved_speaker_m = int(event.get("unresolved_speaker_markets", 0))

        reasons: list[str] = []
        if event.get("discovered_in_events_cache") and snapshot_m == 0:
            reasons.append("EVENT_DISCOVERED_NO_SNAPSHOTS")
        if scored_m == 0:
            reasons.append("NO_ACTION_CARD")
        elif scored_m < open_m:
            reasons.append("PARTIAL_ACTION_CARD")

        if miss_cache_m == open_m and open_m > 0:
            reasons.append("NOT_IN_CACHE")
        elif miss_cache_m > 0:
            reasons.append("PARTIAL_NOT_IN_CACHE")

        if non_phrase_m == open_m and open_m > 0:
            reasons.append("NON_PHRASE_MARKET")
        elif non_phrase_m > 0:
            reasons.append("PARTIAL_NON_PHRASE")
        if unresolved_speaker_m > 0:
            reasons.append("SPEAKER_UNRESOLVED")

        if scored_m > 0:
            if poly_m == 0:
                reasons.append("NO_POLY_MATCH")
            elif poly_m < scored_m:
                reasons.append("PARTIAL_POLY_MATCH")

            if wallet_m == 0:
                reasons.append("NO_WALLET_SIGNAL")
            elif wallet_m < scored_m:
                reasons.append("PARTIAL_WALLET_SIGNAL")

        event["has_scored"] = scored_m > 0
        event["reason_codes"] = sorted(set(reasons))
        if not event["has_scored"]:
            untracked_event_count += 1
            untracked_market_count += int(event.get("open_markets", 0))
        out_events.append(event)

    out_events.sort(
        key=lambda e: (e["has_scored"], -(e.get("open_markets", 0)), e["event_ticker"])
    )
    return {
        "events": out_events,
        "open_event_count": len(out_events),
        "tracked_event_count": sum(1 for e in out_events if e["has_scored"]),
        "untracked_event_count": untracked_event_count,
        "untracked_market_count": untracked_market_count,
        "filtered_status_market_count": filtered_status_market_count,
        "filtered_unknown_speaker_market_count": filtered_unknown_speaker_market_count,
    }


def _fallback_label(et: str) -> str:
    """Generate a readable label from an event ticker when not in the Kalshi cache."""
    import re

    _series_names = {
        "KXTRUMPMENTION": "Trump Mention Market",
        "KXTRUMPMENTIONB": "Trump Mention Market",
        "KXTRUMPSAY": "What Will Trump Say?",
        "KXTRUMPSAYMONTH": "What Will Trump Say? (Monthly)",
        "KXTRUMPSAYNICKNAME": "Trump Nicknames",
        "KXSECPRESSMENTION": "White House Press Secretary Mention",
        "KXMAMDANIMENTION": "Mamdani NYC Mention",
    }
    series = et.rsplit("-", 1)[0] if "-" in et else et
    date_part = et.rsplit("-", 1)[-1] if "-" in et else ""
    base = _series_names.get(series, series)

    if date_part:
        m = re.match(r"(\d{2})([A-Z]{3})(\d{2})", date_part)
        if m:
            month_map = {
                "JAN": "Jan", "FEB": "Feb", "MAR": "Mar", "APR": "Apr",
                "MAY": "May", "JUN": "Jun", "JUL": "Jul", "AUG": "Aug",
                "SEP": "Sep", "OCT": "Oct", "NOV": "Nov", "DEC": "Dec",
            }
            mon = month_map.get(m.group(2), m.group(2))
            base += f" — closes {mon} {m.group(3)}"

    return base


def _build_groups(cards: list[dict], event_meta: dict[str, dict]) -> list[dict]:
    """Group cards by event ticker and attach metadata."""
    buckets: dict[str, list[dict]] = {}
    for c in cards:
        et = c.get("event_ticker", "")
        if not et:
            mid = c["market_id"]
            parts = mid.split("-")
            et = "-".join(parts[:2]) if len(parts) >= 2 else mid
        buckets.setdefault(et, []).append(c)

    groups = []
    for et, group_cards in sorted(buckets.items()):
        meta = event_meta.get(et, {})
        by = sum(1 for c in group_cards if c["side"] == "BUY_YES")
        bn = sum(1 for c in group_cards if c["side"] == "BUY_NO")
        w = sum(1 for c in group_cards if c["side"] == "WATCH")

        best_ev = 0.0
        for c in group_cards:
            ev = float(c["ev_yes"]) if c["side"] == "BUY_YES" else float(c["ev_no"])
            if ev > best_ev:
                best_ev = ev

        label = meta.get("label", "")
        if not label:
            label = _fallback_label(et)
        speaker = meta.get("speaker", "") or group_cards[0].get("speaker", "")

        # Normalize speaker to a canonical ID. Raw name strings like "mehmet oz" or
        # "nick shirley" (extracted from snapshot titles) are not canonical IDs and
        # won't render in the dashboard's SPEAKER_ORDER.  Only known IDs are kept;
        # everything else falls back to "auto" so it appears in the catch-all section.
        _CANONICAL_SPEAKERS = {
            "trump", "leavitt", "mamdani", "fed", "sanders", "starmer",
            "whitehouse", "carney", "homan", "nba", "mlb", "ncaab", "mma",
            "aoc", "hochul", "newsom", "melania", "auto",
        }
        if speaker and speaker not in _CANONICAL_SPEAKERS:
            speaker = "auto"

        # Safety net: verify that each canonical speaker ID belongs to a series
        # actually associated with that speaker.  Protects against the (now-fixed)
        # inference bug where the PHRASE being bet on ("Will X say 'trump'?") was
        # mistakenly used to assign speaker=trump/mamdani/etc.
        _SPEAKER_SERIES_GUARD: dict[str, tuple[str, ...]] = {
            "trump":      ("KXTRUMP", "KXDJT", "KXPRESMENTION"),
            "leavitt":    ("KXLEAVITT", "KXSECPRESS"),
            "mamdani":    ("KXMAMDANI",),
            "homan":      ("KXHOMAN",),
            "carney":     ("KXCARNEY",),
            "starmer":    ("KXSTARMER",),
            "fed":        ("KXFED",),
            "whitehouse": ("KXWHPRESSBRIEFING",),
            "sanders":    ("KXBERNIE", "KXBERN"),
            "aoc":        ("KXAOC",),
            "hochul":     ("KXHOCHUL",),
            "newsom":     ("KXNEWSOM",),
            "melania":    ("KXMELANIA",),
        }
        if speaker in _SPEAKER_SERIES_GUARD:
            if not any(et.startswith(p) for p in _SPEAKER_SERIES_GUARD[speaker]):
                speaker = "auto"

        groups.append({
            "event_ticker": et,
            "label": label,
            "speaker": speaker,
            "close_time": meta.get("close_time", ""),
            "closes_in": meta.get("closes_in", ""),
            "is_closed": meta.get("is_closed", False),
            "total_cards": len(group_cards),
            "buy_yes_count": by,
            "buy_no_count": bn,
            "watch_count": w,
            "best_ev": round(best_ev, 4),
            "cards": group_cards,
        })

    def _sort_key(g: dict) -> tuple:
        is_closed = bool(g.get("is_closed", False))
        close_str = str(g.get("close_time", "") or "")
        close_rank = float("inf")
        if close_str:
            try:
                close_dt = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                if close_dt.tzinfo is None:
                    close_dt = close_dt.replace(tzinfo=timezone.utc)
                close_rank = close_dt.timestamp()
            except Exception:
                close_rank = float("inf")
        # Prioritize open + sooner events first, then stronger edges.
        return (is_closed, close_rank, -float(g.get("best_ev", 0.0)), g["event_ticker"])

    groups.sort(key=_sort_key)
    return groups


def _query_health(conn: sqlite3.Connection) -> dict:
    import datetime as _dt
    _h48 = (_dt.datetime.utcnow() - _dt.timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S")

    # Use recent-only counts (last 48h) to avoid full-table scans on large tables.
    snap_count = conn.execute(
        "SELECT COUNT(*) FROM market_snapshots WHERE ts >= ?", (_h48,)
    ).fetchone()[0]
    latest_snap = conn.execute("SELECT MAX(ts) FROM market_snapshots WHERE ts >= ?", (_h48,)).fetchone()[0] or "-"
    card_count = conn.execute(
        "SELECT COUNT(*) FROM action_cards WHERE ts >= ?", (_h48,)
    ).fetchone()[0]
    latest_card = conn.execute("SELECT MAX(ts) FROM action_cards WHERE ts >= ?", (_h48,)).fetchone()[0] or "-"
    transcript_count = conn.execute("SELECT COUNT(*) FROM transcripts").fetchone()[0]
    latest_transcript = conn.execute("SELECT MAX(ts) FROM transcripts").fetchone()[0] or "-"
    hit_count = conn.execute("SELECT COUNT(*) FROM phrase_hits").fetchone()[0]
    event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    live_events = conn.execute(
        "SELECT COUNT(*) FROM events WHERE speech_state='live'"
    ).fetchone()[0]
    outcome_rows = conn.execute("SELECT COUNT(*) FROM outcome_reviews").fetchone()[0]
    outcome_pnl = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM outcome_reviews"
    ).fetchone()[0]
    # Model version split: BAYESIAN_V1 tag marks new model outcomes
    _bayesian_rows = conn.execute(
        "SELECT COUNT(*) FROM outcome_reviews WHERE reason_codes LIKE '%BAYESIAN_V1%'"
    ).fetchone()[0]
    _bayesian_pnl = conn.execute(
        "SELECT COALESCE(SUM(realized_pnl), 0) FROM outcome_reviews WHERE reason_codes LIKE '%BAYESIAN_V1%'"
    ).fetchone()[0]
    _legacy_rows = outcome_rows - _bayesian_rows
    _legacy_pnl = round(float(outcome_pnl or 0) - float(_bayesian_pnl or 0), 2)

    buy_yes = conn.execute(
        "SELECT COUNT(*) FROM action_cards WHERE side='BUY_YES' AND ts >= ?", (_h48,)
    ).fetchone()[0]
    buy_no = conn.execute(
        "SELECT COUNT(*) FROM action_cards WHERE side='BUY_NO' AND ts >= ?", (_h48,)
    ).fetchone()[0]
    watch = conn.execute(
        "SELECT COUNT(*) FROM action_cards WHERE side='WATCH' AND ts >= ?", (_h48,)
    ).fetchone()[0]

    health = {
        "Snapshots (48h)": snap_count,
        "Latest snapshot": latest_snap,
        "Action cards (48h)": card_count,
        "Latest card": latest_card,
        "BUY_YES cards (48h)": buy_yes,
        "BUY_NO cards (48h)": buy_no,
        "WATCH cards (48h)": watch,
        "Total transcripts": transcript_count,
        "Latest transcript": latest_transcript,
        "Phrase hits": hit_count,
        "Scheduled events": event_count,
        "Live events now": live_events,
        "Outcome review rows": outcome_rows,
        "Realized PnL (all)": round(float(outcome_pnl or 0.0), 4),
        "Legacy model bets": f"{_legacy_rows} (${_legacy_pnl:+.2f})",
        "Bayesian model bets": f"{_bayesian_rows} (${float(_bayesian_pnl or 0):+.2f})",
    }

    # ── BSS metrics from bss_metrics.json (written by backtest_outcomes.py --save) ──
    bss_path = Path("data/bss_metrics.json")
    if bss_path.exists():
        try:
            bss_data = json.loads(bss_path.read_text(encoding="utf-8"))
            updated = (bss_data.get("updated_at") or "-")[:16]

            def _bss_label(v: float | None) -> str:
                if v is None:
                    return "N/A"
                if v > 0.05:
                    return f"{v:+.4f} ✅"
                if v > 0.01:
                    return f"{v:+.4f} ✓"
                if v > -0.01:
                    return f"{v:+.4f} ~"
                return f"{v:+.4f} ❌"

            wf = bss_data.get("walk_forward", {})
            if wf:
                health["Perf: BSS walk-forward"] = _bss_label(wf.get("bss_vs_market"))
                health["Perf: Brier walk-fwd"]   = wf.get("brier_model")
                ci = wf.get("bss_ci_90")
                if ci:
                    health["Perf: BSS CI (90%)"] = f"[{ci[0]:+.4f}, {ci[1]:+.4f}]"

            live_bss = bss_data.get("live", {})
            if live_bss:
                health["Perf: BSS live outcomes"] = _bss_label(live_bss.get("bss_vs_market"))
                health["Perf: Brier live"]        = live_bss.get("brier_model")
                wr = live_bss.get("win_rate")
                nb = live_bss.get("n_bets")
                if wr is not None and nb:
                    health["Perf: Live win rate"]  = f"{wr:.1%} ({nb} bets)"

            health["Perf: last run"] = updated
        except Exception:
            pass

    # Rolling 30-day BSS from outcome_reviews (quick inline compute)
    try:
        import datetime as _dt2
        _cutoff = (_dt2.datetime.utcnow() - _dt2.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S")
        _rows = conn.execute(
            "SELECT p_literal, yes_ask, no_ask, outcome FROM outcome_reviews "
            "WHERE resolved_ts >= ? AND outcome IN ('yes','no')",
            (_cutoff,),
        ).fetchall()
        if len(_rows) >= 10:
            _mp, _mkt, _ac = [], [], []
            for _r in _rows:
                _mp.append(float(_r[0]))
                _mkt.append((float(_r[1]) + (1.0 - float(_r[2]))) / 2.0)
                _ac.append(int(_r[3] == "yes"))
            _bs_m  = sum((p - a) ** 2 for p, a in zip(_mp,  _ac)) / len(_ac)
            _bs_mk = sum((p - a) ** 2 for p, a in zip(_mkt, _ac)) / len(_ac)
            _bss30 = round(1.0 - _bs_m / _bs_mk, 4) if _bs_mk > 0 else None
            if _bss30 is not None:
                _label = "✅" if _bss30 > 0.05 else ("✓" if _bss30 > 0.01 else
                         ("~" if _bss30 > -0.01 else "❌"))
                health["Perf: BSS (30d live)"] = f"{_bss30:+.4f} {_label}  n={len(_rows)}"
    except Exception:
        pass

    # ── DB file health ────────────────────────────────────────────────────────
    import os as _os
    _db_path = Path("data/edge.db")
    if _db_path.exists():
        _db_mb = round(_db_path.stat().st_size / 1024 / 1024, 1)
        health["DB: size"] = f"{_db_mb} MB"
    _wal = Path("data/edge.db-wal")
    health["DB: WAL"] = f"{round(_wal.stat().st_size/1024,0):.0f} KB" if _wal.exists() else "none"
    _shm = Path("data/edge.db-shm")
    health["DB: SHM"] = "present" if _shm.exists() else "none"

    # ── Runner process ─────────────────────────────────────────────────────────
    _lock = Path("data/runner.lock")
    if _lock.exists():
        try:
            _pid = int(_lock.read_text().strip())
            health["Runner: PID"] = str(_pid)
            try:
                _os.kill(_pid, 0)
                health["Runner: status"] = "alive"
            except ProcessLookupError:
                health["Runner: status"] = "dead (stale lock)"
        except Exception:
            health["Runner: status"] = "lock exists (unreadable)"
    else:
        health["Runner: status"] = "no lock file"

    # ── Signal file freshness (age in minutes) ─────────────────────────────────
    import datetime as _dt4
    def _age_min(p: str) -> float | None:
        pp = Path(p)
        if not pp.exists():
            return None
        return (_dt4.datetime.utcnow() - _dt4.datetime.utcfromtimestamp(pp.stat().st_mtime)).total_seconds() / 60

    health["Files: signal_context"]  = _age_min("data/signal_context.json")
    health["Files: wh_schedule"]     = _age_min("data/wh_schedule.json")
    health["Files: polymarket"]      = _age_min("data/polymarket_prices.json")
    health["Files: truth_social"]    = _age_min("data/truth_social_posts.json")
    health["Files: bias_map"]        = _age_min("data/bias_map.json")
    health["Files: rolling_rates"]   = _age_min("data/rolling_hit_rates.json")
    health["Files: hazard_rates"]    = _age_min("data/phrase_hazard_rates.json")

    # Truth Social: post count and newest post age
    _ts_path = Path("data/truth_social_posts.json")
    if _ts_path.exists():
        try:
            _ts = json.loads(_ts_path.read_text(encoding="utf-8"))
            _posts = _ts if isinstance(_ts, list) else _ts.get("posts", [])
            health["TS: post count"] = len(_posts)
            import datetime as _dt5
            _now = _dt5.datetime.now(_dt5.timezone.utc)
            _ages = []
            for _p in _posts:
                _raw = _p.get("posted_at") or _p.get("created_at")
                if _raw:
                    try:
                        _dt_p = _dt5.datetime.fromisoformat(_raw.replace("Z", "+00:00"))
                        _ages.append((_now - _dt_p).total_seconds() / 60)
                    except Exception:
                        pass
            if _ages:
                health["TS: newest post"] = f"{min(_ages):.0f} min ago"
        except Exception:
            pass

    # ── Regime alerts ──────────────────────────────────────────────────────────
    _regime_path = Path("data/regime_alerts.json")
    if _regime_path.exists():
        try:
            _ra = json.loads(_regime_path.read_text(encoding="utf-8"))
            health["Regime: total alerts"] = _ra.get("total_alerts", 0)
            health["Regime: high severity"] = _ra.get("high_severity", 0)
            _alerts = _ra.get("alerts", [])
            health["Regime: alerts_raw"] = _alerts[:6]  # pass raw for JS rendering
        except Exception:
            pass

    # ── Threshold optimization ─────────────────────────────────────────────────
    _thresh_path = Path("data/threshold_optimization.json")
    if _thresh_path.exists():
        try:
            _traw = json.loads(_thresh_path.read_text(encoding="utf-8"))
            # best_params may be null; fall back to top_results[0]
            _t = _traw.get("best_params") or (_traw.get("top_results") or [None])[0] or {}
            health["Thresh: computed"] = (_traw.get("optimized_at") or "-")[:16]
            health["Thresh: outcomes used"] = _traw.get("n_outcomes")
            health["Thresh: ev_threshold"] = _t.get("ev_threshold")
            health["Thresh: kelly_min"] = _t.get("kelly_min")
            health["Thresh: no_ev_premium"] = _t.get("no_ev_premium")
            _wr = _t.get("win_pct")
            _n = _t.get("n")
            if _wr is not None:
                health["Thresh: win_pct"] = f"{_wr:.1f}%  (n={_n})" if _n else f"{_wr:.1f}%"
            _roi = _t.get("roi")
            if _roi is not None:
                health["Thresh: ROI"] = f"{_roi:.2f}x"
        except Exception:
            pass

    usage_path = Path("data/x_usage.json")
    if usage_path.exists():
        try:
            usage = json.loads(usage_path.read_text(encoding="utf-8"))
            posts_read = int(usage.get("posts_read", 0))
            users_read = int(usage.get("users_read", 0))
            est_spend = posts_read * 0.005 + users_read * 0.010
            health["X month"] = usage.get("month", "-")
            health["X posts read"] = posts_read
            health["X users read"] = users_read
            health["X est spend"] = f"${est_spend:.2f}"
        except Exception:
            pass

    # Platt calibration stats from calibration.json cache
    calib_path = Path("data/calibration.json")
    calib: dict = {}
    if calib_path.exists():
        try:
            calib = json.loads(calib_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    if calib:
        import math as _math
        a = float(calib.get("a", 0))
        b = float(calib.get("b", 0))
        n = int(calib.get("n_samples", 0))
        fitted_at = calib.get("fitted_at", "-")
        # Average shift at the typical mid-range value p=0.40
        try:
            logit_040 = _math.log(0.40 / 0.60)
            cal_040 = 1.0 / (1.0 + _math.exp(-(a * logit_040 + b)))
            shift_040 = round(cal_040 - 0.40, 3)
        except Exception:
            shift_040 = None
        health["Calib: params"] = f"a={a:.3f}  b={b:.3f}"
        health["Calib: fitted on"] = f"{n} outcomes"
        health["Calib: last refit"] = fitted_at[:16] if fitted_at and fitted_at != "-" else "-"
        health["Calib: shift @ p=0.40"] = f"{shift_040:+.3f}" if shift_040 is not None else "-"
    else:
        health["Calib: params"] = "not fitted"

    return health


def _rolling_entry_from_rows(w_rows: list) -> dict:
    """Aggregate one rolling window from outcome rows (caller filters by date / speaker)."""
    if not w_rows:
        return {"n": 0}
    w_wins = sum(
        1 for r in w_rows
        if (r["side"] == "BUY_YES" and r["outcome"] == "yes")
        or (r["side"] == "BUY_NO" and r["outcome"] == "no")
    )
    w_pnl = round(sum(r["realized_pnl"] or 0 for r in w_rows), 2)
    by_side_w: dict[str, dict] = {}
    for r in w_rows:
        s = r["side"]
        if s not in by_side_w:
            by_side_w[s] = {"wins": 0, "total": 0, "pnl": 0.0}
        by_side_w[s]["total"] += 1
        by_side_w[s]["pnl"] = round(by_side_w[s]["pnl"] + (r["realized_pnl"] or 0), 2)
        if (s == "BUY_YES" and r["outcome"] == "yes") or (s == "BUY_NO" and r["outcome"] == "no"):
            by_side_w[s]["wins"] += 1
    for v in by_side_w.values():
        v["win_rate"] = round(v["wins"] / v["total"], 3) if v["total"] else 0
    bss_w: float | None = None
    try:
        mp_l, mkt_l, ac_l = [], [], []
        for r in w_rows:
            mp_l.append(float(r["p_literal"] or 0))
            mkt_l.append((float(r["yes_ask"] or 0.5) + (1.0 - float(r["no_ask"] or 0.5))) / 2.0)
            ac_l.append(int(r["outcome"] == "yes"))
        bs_m = sum((p - a) ** 2 for p, a in zip(mp_l, ac_l)) / len(ac_l)
        bs_mk = sum((p - a) ** 2 for p, a in zip(mkt_l, ac_l)) / len(ac_l)
        bss_w = round(1.0 - bs_m / bs_mk, 4) if bs_mk > 0 else None
    except Exception:
        pass
    return {
        "n": len(w_rows),
        "wins": w_wins,
        "win_rate": round(w_wins / len(w_rows), 3),
        "pnl": w_pnl,
        "bss": bss_w,
        "by_side": by_side_w,
    }


def _empty_outcome_speaker_metrics() -> dict:
    roll = {f"{d}d": {"n": 0} for d in (7, 30, 60, 90)}
    return {
        "total": 0,
        "wins": 0,
        "win_rate": 0.0,
        "total_pnl": 0.0,
        "bss": None,
        "by_side": {},
        "rolling": roll,
        "recent": [],
        "top_phrases": [],
    }


def _merge_outcome_speaker_canonical(
    by_speaker: dict[str, dict],
) -> tuple[dict[str, dict], list[str]]:
    out = dict(by_speaker)
    canon = set(PERFORMANCE_SPEAKER_ORDER)
    for spk in PERFORMANCE_SPEAKER_ORDER:
        if spk not in out:
            out[spk] = _empty_outcome_speaker_metrics()
    extra = sorted(k for k in out.keys() if k not in canon)
    return out, list(PERFORMANCE_SPEAKER_ORDER) + extra


def _query_outcomes(conn: sqlite3.Connection) -> dict:
    """Aggregate outcome_reviews into win-rate / P&L stats for the Outcomes tab."""
    import datetime as _dt
    rows = conn.execute("""
        SELECT side, outcome, phrase, speaker,
               json_extract(raw_json, '$.event.event_type') as event_type,
               realized_pnl, prediction_ts, reason_codes,
               p_literal, yes_ask, no_ask
        FROM outcome_reviews
        ORDER BY prediction_ts DESC
    """).fetchall()

    if not rows:
        rolling_empty = {f"{d}d": {"n": 0} for d in (7, 30, 60, 90)}
        merged, canon = _merge_outcome_speaker_canonical({})
        return {
            "total": 0,
            "wins": 0,
            "win_rate": 0,
            "total_pnl": 0.0,
            "by_side": {},
            "by_event_type": {},
            "by_phrase": [],
            "recent": [],
            "rolling": rolling_empty,
            "by_speaker": merged,
            "canonical_speakers": canon,
        }

    total = len(rows)
    wins = sum(1 for r in rows if (r["side"] == "BUY_YES" and r["outcome"] == "yes")
                                   or (r["side"] == "BUY_NO" and r["outcome"] == "no"))
    total_pnl = round(sum(r["realized_pnl"] or 0 for r in rows), 2)

    # By side
    by_side: dict[str, dict] = {}
    for r in rows:
        s = r["side"]
        if s not in by_side:
            by_side[s] = {"wins": 0, "total": 0, "pnl": 0.0}
        by_side[s]["total"] += 1
        by_side[s]["pnl"] = round(by_side[s]["pnl"] + (r["realized_pnl"] or 0), 2)
        won = (s == "BUY_YES" and r["outcome"] == "yes") or (s == "BUY_NO" and r["outcome"] == "no")
        if won:
            by_side[s]["wins"] += 1
    for v in by_side.values():
        v["win_rate"] = round(v["wins"] / v["total"], 3) if v["total"] else 0

    # By event type
    by_etype: dict[str, dict] = {}
    for r in rows:
        et = r["event_type"] or "unknown"
        if et not in by_etype:
            by_etype[et] = {"wins": 0, "total": 0, "pnl": 0.0}
        by_etype[et]["total"] += 1
        by_etype[et]["pnl"] = round(by_etype[et]["pnl"] + (r["realized_pnl"] or 0), 2)
        won = (r["side"] == "BUY_YES" and r["outcome"] == "yes") or (r["side"] == "BUY_NO" and r["outcome"] == "no")
        if won:
            by_etype[et]["wins"] += 1
    for v in by_etype.values():
        v["win_rate"] = round(v["wins"] / v["total"], 3) if v["total"] else 0

    # By phrase (min 2 resolved)
    # Track yes_count/no_count to determine dominant side and flag MIXED bets.
    by_phrase: dict[str, dict] = {}
    for r in rows:
        ph = r["phrase"] or "?"
        if ph not in by_phrase:
            by_phrase[ph] = {"wins": 0, "total": 0, "pnl": 0.0, "yes_count": 0, "no_count": 0}
        by_phrase[ph]["total"] += 1
        by_phrase[ph]["pnl"] = round(by_phrase[ph]["pnl"] + (r["realized_pnl"] or 0), 2)
        won = (r["side"] == "BUY_YES" and r["outcome"] == "yes") or (r["side"] == "BUY_NO" and r["outcome"] == "no")
        if won:
            by_phrase[ph]["wins"] += 1
        if r["side"] == "BUY_YES":
            by_phrase[ph]["yes_count"] += 1
        else:
            by_phrase[ph]["no_count"] += 1
    phrase_rows = []
    for ph, v in by_phrase.items():
        if v["total"] >= 2:
            yes_c = v["yes_count"]
            no_c = v["no_count"]
            # MIXED if we've bet both sides; otherwise show dominant side
            if yes_c > 0 and no_c > 0:
                side_display = "MIXED"
            elif yes_c > 0:
                side_display = "BUY_YES"
            else:
                side_display = "BUY_NO"
            phrase_rows.append({
                "phrase": ph,
                "wins": v["wins"],
                "total": v["total"],
                "pnl": v["pnl"],
                "win_rate": round(v["wins"] / v["total"], 3),
                "side": side_display,
            })
    phrase_rows.sort(key=lambda x: (-x["total"], x["win_rate"]))

    # Recent bets (last 20)
    recent = []
    for r in list(rows)[:20]:
        won = (r["side"] == "BUY_YES" and r["outcome"] == "yes") or (r["side"] == "BUY_NO" and r["outcome"] == "no")
        recent.append({
            "phrase": r["phrase"],
            "side": r["side"],
            "outcome": r["outcome"],
            "pnl": round(r["realized_pnl"] or 0, 2),
            "won": won,
            "ts": (r["prediction_ts"] or "")[:16],
            "event_type": r["event_type"] or "?",
        })

    # ── Rolling windows (7 / 30 / 60 / 90 days) ─────────────────────────────
    rolling: dict[str, dict] = {}
    now_utc = _dt.datetime.utcnow()
    for days in (7, 30, 60, 90):
        cutoff = (now_utc - _dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        w_rows = conn.execute(
            """SELECT side, outcome, p_literal, yes_ask, no_ask, realized_pnl
               FROM outcome_reviews
               WHERE prediction_ts >= ? AND outcome IN ('yes','no')""",
            (cutoff,),
        ).fetchall()
        rolling[f"{days}d"] = _rolling_entry_from_rows(w_rows)

    # ── By speaker ───────────────────────────────────────────────────────────
    by_spk_raw: dict[str, dict] = {}
    for r in rows:
        spk = (r["speaker"] or "auto").lower().strip() or "auto"
        if spk not in by_spk_raw:
            by_spk_raw[spk] = {
                "wins": 0, "total": 0, "pnl": 0.0,
                "phrases": {},
                "_pl": [], "_ya": [], "_na": [], "_ac": [],
                "_rows": [],
                "by_side": {},
            }
        sv = by_spk_raw[spk]
        sv["total"] += 1
        sv["pnl"] = round(sv["pnl"] + (r["realized_pnl"] or 0), 2)
        won = (r["side"] == "BUY_YES" and r["outcome"] == "yes") or (
              r["side"] == "BUY_NO" and r["outcome"] == "no")
        if won:
            sv["wins"] += 1
        sv["_rows"].append(r)
        side_k = r["side"]
        if side_k not in sv["by_side"]:
            sv["by_side"][side_k] = {"wins": 0, "total": 0, "pnl": 0.0}
        sv["by_side"][side_k]["total"] += 1
        sv["by_side"][side_k]["pnl"] = round(
            sv["by_side"][side_k]["pnl"] + (r["realized_pnl"] or 0), 2
        )
        if won:
            sv["by_side"][side_k]["wins"] += 1
        # Per-phrase breakdown per speaker
        ph = r["phrase"] or "?"
        if ph not in sv["phrases"]:
            sv["phrases"][ph] = {"wins": 0, "total": 0, "pnl": 0.0}
        sv["phrases"][ph]["total"] += 1
        sv["phrases"][ph]["pnl"] = round(sv["phrases"][ph]["pnl"] + (r["realized_pnl"] or 0), 2)
        if won:
            sv["phrases"][ph]["wins"] += 1
        # BSS data collection
        if (r["p_literal"] is not None and r["yes_ask"] is not None
                and r["outcome"] in ("yes", "no")):
            sv["_pl"].append(float(r["p_literal"] or 0))
            sv["_ya"].append(float(r["yes_ask"] or 0.5))
            sv["_na"].append(float(r["no_ask"] or 0.5))
            sv["_ac"].append(int(r["outcome"] == "yes"))

    by_speaker: dict[str, dict] = {}
    for spk, sv in by_spk_raw.items():
        wr = round(sv["wins"] / sv["total"], 3) if sv["total"] else 0
        # Compute BSS for this speaker
        bss_val: float | None = None
        if len(sv["_pl"]) >= 3:
            try:
                pl = sv["_pl"]; ya = sv["_ya"]; na = sv["_na"]; ac = sv["_ac"]
                mkt = [(ya[i] + (1.0 - na[i])) / 2.0 for i in range(len(pl))]
                bs_m  = sum((p - a) ** 2 for p, a in zip(pl, ac)) / len(ac)
                bs_mk = sum((p - a) ** 2 for p, a in zip(mkt, ac)) / len(ac)
                bss_val = round(1.0 - bs_m / bs_mk, 4) if bs_mk > 0 else None
            except Exception:
                pass
        # Top 5 phrases sorted by bet count then win rate
        top_phrases = sorted(
            sv["phrases"].items(), key=lambda x: (-x[1]["total"], -x[1]["wins"])
        )[:15]
        by_side_out: dict[str, dict] = {}
        for sk, sd in sv["by_side"].items():
            by_side_out[sk] = {
                "wins": sd["wins"],
                "total": sd["total"],
                "pnl": sd["pnl"],
                "win_rate": round(sd["wins"] / sd["total"], 3) if sd["total"] else 0,
            }
        spk_rolling: dict[str, dict] = {}
        for days in (7, 30, 60, 90):
            cutoff = (now_utc - _dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
            w_spk = [
                x for x in sv["_rows"]
                if (x["prediction_ts"] or "") >= cutoff and x["outcome"] in ("yes", "no")
            ]
            spk_rolling[f"{days}d"] = _rolling_entry_from_rows(w_spk)
        sl_desc = sorted(
            sv["_rows"], key=lambda x: x["prediction_ts"] or "", reverse=True
        )[:15]
        spk_recent: list[dict] = []
        for rr in sl_desc:
            won_r = (rr["side"] == "BUY_YES" and rr["outcome"] == "yes") or (
                rr["side"] == "BUY_NO" and rr["outcome"] == "no"
            )
            spk_recent.append({
                "phrase": rr["phrase"],
                "side": rr["side"],
                "outcome": rr["outcome"],
                "pnl": round(rr["realized_pnl"] or 0, 2),
                "won": won_r,
                "ts": (rr["prediction_ts"] or "")[:16],
                "event_type": rr["event_type"] or "?",
            })
        by_speaker[spk] = {
            "total": sv["total"],
            "wins": sv["wins"],
            "win_rate": wr,
            "total_pnl": sv["pnl"],
            "bss": bss_val,
            "by_side": by_side_out,
            "rolling": spk_rolling,
            "recent": spk_recent,
            "top_phrases": [
                {
                    "phrase": ph,
                    "wins": d["wins"],
                    "total": d["total"],
                    "pnl": d["pnl"],
                    "win_rate": round(d["wins"] / d["total"], 3) if d["total"] else 0,
                }
                for ph, d in top_phrases
            ],
        }

    by_speaker, canonical_speakers = _merge_outcome_speaker_canonical(by_speaker)

    return {
        "total": total,
        "wins": wins,
        "win_rate": round(wins / total, 3) if total else 0,
        "total_pnl": total_pnl,
        "by_side": by_side,
        "by_event_type": by_etype,
        "by_phrase": phrase_rows[:40],
        "recent": recent,
        "rolling": rolling,
        "by_speaker": by_speaker,
        "canonical_speakers": canonical_speakers,
    }


_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# Team abbreviation → display name (matches Kalshi market-ID codes).
_MLB_TEAMS: dict[str, str] = {
    "ARI": "Arizona", "AZ": "Arizona",
    "ATL": "Atlanta", "BAL": "Baltimore", "BOS": "Boston",
    "CHC": "Chi Cubs", "CHW": "Chi Sox", "CWS": "Chi Sox",
    "CIN": "Cincinnati", "CLE": "Cleveland", "COL": "Colorado",
    "DET": "Detroit", "HOU": "Houston",
    "KC": "Kansas City", "KCA": "Kansas City", "KCR": "Kansas City",
    "LAA": "LA Angels", "LAD": "LA Dodgers",
    "MIA": "Miami", "MIL": "Milwaukee", "MIN": "Minnesota",
    "NYM": "NY Mets", "NYY": "NY Yankees",
    "OAK": "Oakland", "PHI": "Philadelphia", "PIT": "Pittsburgh",
    "SD": "San Diego", "SDP": "San Diego",
    "SEA": "Seattle", "SF": "SF Giants", "SFG": "SF Giants",
    "STL": "St. Louis", "TB": "Tampa Bay", "TBR": "Tampa Bay",
    "TEX": "Texas", "TOR": "Toronto", "WSH": "Washington",
}

_NBA_TEAMS: dict[str, str] = {
    "ATL": "Atlanta", "BOS": "Boston", "BKN": "Brooklyn",
    "CHA": "Charlotte", "CHI": "Chicago", "CLE": "Cleveland",
    "DAL": "Dallas", "DEN": "Denver", "DET": "Detroit",
    "GSW": "Golden State", "HOU": "Houston", "IND": "Indiana",
    "LAC": "LA Clippers", "LAL": "LA Lakers",
    "MEM": "Memphis", "MIA": "Miami", "MIL": "Milwaukee",
    "MIN": "Minnesota", "NOP": "New Orleans", "NYK": "NY Knicks",
    "OKC": "Oklahoma City", "ORL": "Orlando",
    "PHI": "Philadelphia", "PHX": "Phoenix", "POR": "Portland",
    "SAC": "Sacramento", "SAS": "San Antonio", "TOR": "Toronto",
    "UTA": "Utah", "WAS": "Washington",
}


def _split_team_codes(s: str, lookup: dict[str, str]) -> tuple[str, str] | None:
    """Split a concatenated team code string (e.g. 'NYMSF', 'CLESEA') into two
    known team codes by trying every split position.  Returns (t1, t2) or None."""
    for i in range(2, len(s) - 1):
        t1, t2 = s[:i], s[i:]
        if t1 in lookup and t2 in lookup:
            return t1, t2
    return None


def _sport_game_label(
    raw_after_prefix: str,
    game_date: str | None,
    sport_key: str,
    prompt: str,
) -> str:
    """Build a human-readable game label like 'NY Mets vs SF Giants — Apr 2'."""
    import re as _re

    # Friendly date suffix: "2026-04-02" → "Apr 2"
    date_sfx = ""
    if game_date:
        try:
            from datetime import datetime as _dtp
            date_sfx = " — " + _dtp.strptime(game_date, "%Y-%m-%d").strftime("%b %-d")
        except ValueError:
            pass

    # Strip date (YYMONDD) and optional game time (HHMM) from raw suffix
    m = _re.match(r"^\d{2}[A-Z]{3}\d{2}(\d{4})?(.+)$", raw_after_prefix)
    teams_raw = m.group(2) if m else raw_after_prefix

    # Try team abbreviation lookup
    lookup = _NBA_TEAMS if sport_key == "nba" else _MLB_TEAMS if sport_key in ("mlb", "wbc") else {}
    if lookup:
        pair = _split_team_codes(teams_raw, lookup)
        if pair:
            return f"{lookup[pair[0]]} vs {lookup[pair[1]]}{date_sfx}"

    # NBA fallback: extract matchup from market prompt
    if sport_key == "nba" and prompt:
        pm = _re.search(r"during (.+?) Professional", prompt)
        if pm:
            matchup = pm.group(1).strip()
            # Clean up "Los Angeles L" → "LA Lakers", "Los Angeles C" → "LA Clippers"
            matchup = matchup.replace("Los Angeles L", "LA Lakers").replace("Los Angeles C", "LA Clippers")
            return f"{matchup}{date_sfx}"

    # Last resort: just use the raw team string + date
    return f"{teams_raw}{date_sfx}"


def _sport_game_meta_from_db(
    conn: sqlite3.Connection,
    like_pattern: str,
    strip_prefix: str,
    sport_key: str = "",
) -> dict[str, dict]:
    """Generic helper: discover event tickers for any sport from the markets table.

    ``like_pattern`` — e.g. ``"KXNCAABMENTION%"``
    ``strip_prefix``  — e.g. ``"KXNCAABMENTION-"`` (removed from labels)
    """
    import re as _re

    rows = conn.execute(
        f"SELECT market_id FROM markets WHERE market_id LIKE '{like_pattern}'"
    ).fetchall()
    tickers: set[str] = set()
    for (mid,) in rows:
        parts = str(mid).split("-")
        if len(parts) >= 2:
            tickers.add("-".join(parts[:2]))

    # Fetch one prompt per event ticker for NBA fallback label parsing
    prompt_rows = conn.execute(
        f"SELECT market_id, prompt FROM markets WHERE market_id LIKE '{like_pattern}'"
    ).fetchall()
    prompt_by_et: dict[str, str] = {}
    for mid, prompt in prompt_rows:
        parts = str(mid).split("-")
        et = "-".join(parts[:2]) if len(parts) >= 2 else mid
        if et not in prompt_by_et and prompt:
            prompt_by_et[et] = prompt

    out: dict[str, dict] = {}
    for et in sorted(tickers):
        game_date: str | None = None
        dm = _re.search(r"(\d{2})([A-Z]{3})(\d{2})", et)
        if dm:
            yr, mon_str, day = dm.group(1), dm.group(2), dm.group(3)
            mon = _MONTHS.get(mon_str)
            if mon:
                game_date = f"20{yr}-{mon:02d}-{int(day):02d}"
        raw_after_prefix = et.replace(strip_prefix, "")
        label = _sport_game_label(
            raw_after_prefix, game_date, sport_key, prompt_by_et.get(et, "")
        )
        out[et] = {
            "event_ticker": et,
            "label":        label,
            "game_date":    game_date,
            "arena":        "",
            "arena_phrase": None,
            "away_team":    "",
            "home_team":    "",
        }
    return out


def _build_sport_games(
    cards: list[dict],
    games_meta: dict[str, dict],
    today: str,
    sport_key: str,
) -> list[dict]:
    """Group cards by event ticker and produce the games list for one sport."""
    import json as _json
    import re as _re2

    buckets: dict[str, list[dict]] = {}
    for c in cards:
        mid = c.get("market_id", "")
        parts = mid.split("-")
        et = "-".join(parts[:2]) if len(parts) >= 2 else mid
        buckets.setdefault(et, []).append(c)

    for et in games_meta:
        buckets.setdefault(et, [])

    games_out = []
    for et in sorted(buckets.keys()):
        meta = games_meta.get(et, {})
        gc = buckets[et]
        game_date = meta.get("game_date", "")
        is_ended = bool(game_date and game_date < today)

        arena_override_count = 0
        if sport_key == "nba":
            _safe = _re2.sub(r"[^\w\-]", "_", f"auto:nba:{et}") + ".json"
            sig_path = _REPO_ROOT / "data" / "event_signals" / _safe
            if sig_path.exists():
                try:
                    sigs = _json.loads(sig_path.read_text())
                    arena_override_count = len(sigs.get("p_override", {}))
                except Exception:
                    pass

        by = sum(1 for c in gc if c["side"] == "BUY_YES")
        bn = sum(1 for c in gc if c["side"] == "BUY_NO")
        w  = sum(1 for c in gc if c["side"] == "WATCH")

        best_ev = 0.0
        for c in gc:
            ev = float(c["ev_yes"]) if c["side"] == "BUY_YES" else float(c["ev_no"])
            if ev > best_ev:
                best_ev = ev

        games_out.append({
            "event_ticker":         et,
            "label":                meta.get("label", et),
            "game_date":            game_date,
            "arena":                meta.get("arena", ""),
            "arena_phrase":         meta.get("arena_phrase"),
            "away_team":            meta.get("away_team", ""),
            "home_team":            meta.get("home_team", ""),
            "is_ended":             is_ended,
            "arena_override_count": arena_override_count,
            "total_cards":          len(gc),
            "buy_yes_count":        by,
            "buy_no_count":         bn,
            "watch_count":          w,
            "best_ev":              round(best_ev, 4),
            "cards":                gc,
        })

    games_out.sort(key=lambda g: (g["is_ended"], g["game_date"] or "", -g["best_ev"]))
    return games_out


def _query_sports(
    conn: sqlite3.Connection,
    nba_cards: list[dict],
    ncaab_cards: list[dict],
    mlb_cards: list[dict],
    fight_cards: list[dict],
    wbc_cards: list[dict],
) -> dict:
    """Build Sports tab data for all leagues.

    Each card list must be loaded with ``apply_blocked_filter=False`` — the main
    Markets tab blocks all sports; this tab is their dedicated surface.
    """
    import json as _json
    from datetime import datetime as _dt, timezone as _tz

    today = _dt.now(_tz.utc).date().isoformat()

    # ── NBA ───────────────────────────────────────────────────────────────────
    schedule_path = _REPO_ROOT / "data" / "nba_schedule.json"
    schedule_data: dict = {}
    if schedule_path.exists():
        try:
            schedule_data = _json.loads(schedule_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    nba_meta: dict[str, dict] = {
        g["event_ticker"]: g
        for g in schedule_data.get("games", [])
        if g.get("event_ticker")
    }
    for et, m in _sport_game_meta_from_db(conn, "KXNBAMENTION%", "KXNBAMENTION-", "nba").items():
        nba_meta.setdefault(et, m)

    # ── Generic sports: NCAAB, MLB, Fight, WBC ────────────────────────────────
    ncaab_meta = _sport_game_meta_from_db(conn, "KXNCAABMENTION%", "KXNCAABMENTION-", "ncaab")
    mlb_meta   = _sport_game_meta_from_db(conn, "KXMLBMENTION%",   "KXMLBMENTION-",   "mlb")
    fight_meta = _sport_game_meta_from_db(conn, "KXFIGHT%",         "KXFIGHT-",        "fight")
    wbc_meta   = _sport_game_meta_from_db(conn, "KXWBC%",           "KXWBC-",          "wbc")

    _sport_defs = [
        ("nba",   "NBA",       "🏀", nba_meta,   nba_cards,   schedule_data.get("games", [])),
        ("ncaab", "NCAAB",     "🏀", ncaab_meta, ncaab_cards, []),
        ("mlb",   "MLB",       "⚾", mlb_meta,   mlb_cards,   []),
        ("fight", "MMA/Fight", "🥊", fight_meta, fight_cards, []),
        ("wbc",   "WBC",       "⚾", wbc_meta,   wbc_cards,   []),
    ]

    sports_out = []
    for sport_key, label, icon, meta, cards, schedule in _sport_defs:
        games = _build_sport_games(cards, meta, today, sport_key)
        if not games:
            continue
        sports_out.append({
            "sport":    sport_key,
            "label":    label,
            "icon":     icon,
            "games":    games,
            "schedule": schedule,
        })

    return {"sports": sports_out}


_INTEL_CATEGORY_META: dict[str, dict] = {
    "trump":      {"label": "Trump Mentions",      "icon": "🇺🇸"},
    "leavitt":    {"label": "Leavitt / WH Press",  "icon": "🏛"},
    "whitehouse": {"label": "WH Press Briefing",   "icon": "🏛"},
    "mamdani":    {"label": "Mamdani",             "icon": "🗽"},
    "sanders":    {"label": "Bernie Sanders",      "icon": "🗳"},
    "starmer":    {"label": "Starmer",             "icon": "🇬🇧"},
    "carney":     {"label": "Carney",              "icon": "🇨🇦"},
    "homan":      {"label": "Homan",               "icon": "🛡"},
    "walz":       {"label": "Walz",                "icon": "🗳"},
    "aoc":        {"label": "AOC",                 "icon": "🗳"},
    "hochul":     {"label": "Hochul",              "icon": "🗽"},
    "newsom":     {"label": "Newsom",              "icon": "🌴"},
    "melania":    {"label": "Melania",             "icon": "🗳"},
    "fed":        {"label": "Fed / Powell",        "icon": "🏦"},
    "jensen":     {"label": "Jensen Huang",        "icon": "💡"},
    "witness":    {"label": "Congress / Witnesses","icon": "🏛"},
    "ncaab":      {"label": "NCAAB",               "icon": "🏀"},
    "nba":        {"label": "NBA",                 "icon": "🏀"},
    "mlb":        {"label": "MLB",                 "icon": "⚾"},
    "mma":        {"label": "MMA / Fight",         "icon": "🥊"},
    "auto":       {"label": "Other Markets",       "icon": "📊"},
    "other":      {"label": "Other Markets",       "icon": "📊"},
}


def _query_intelligence(all_cards: list[dict], llm_analysis: dict) -> list[dict]:
    """Group active action cards by speaker/category and enrich with LLM assessment data.

    Returns a list of category objects (one per speaker group with any cards),
    ordered by PERFORMANCE_SPEAKER_ORDER.  Each object contains its phrases with
    the LLM boost/suppress, confidence, and reasoning attached.
    """
    import json as _json

    # Build phrase → LLM assessment lookup (case-insensitive)
    assess_by_phrase: dict[str, dict] = {}
    for a in llm_analysis.get("assessments", []):
        phrase = (a.get("phrase") or "").lower().strip()
        if phrase:
            assess_by_phrase[phrase] = a

    # Group cards by speaker (from raw_json → event.speaker, or infer from market_id)
    speaker_buckets: dict[str, list[dict]] = {}
    for c in all_cards:
        raw: dict = {}
        try:
            raw = _json.loads(c.get("raw_json") or "{}")
        except Exception:
            pass
        speaker = (
            raw.get("event", {}).get("speaker")
            or raw.get("speaker")
            or ""
        ).lower().strip()
        if not speaker:
            # Infer from market_id prefix
            mid = c.get("market_id", "").upper()
            if mid.startswith("KXTRUMP"):
                speaker = "trump"
            elif mid.startswith("KXLEAVITT"):
                speaker = "leavitt"
            elif mid.startswith("KXFED"):
                speaker = "fed"
            elif mid.startswith("KXSTARMER"):
                speaker = "starmer"
            elif mid.startswith("KXSANDERS") or mid.startswith("KXBERNIE"):
                speaker = "sanders"
            elif mid.startswith("KXNBA"):
                speaker = "nba"
            elif mid.startswith("KXNCAAB"):
                speaker = "ncaab"
            elif mid.startswith("KXMLB"):
                speaker = "mlb"
            elif mid.startswith("KXFIGHT") or mid.startswith("KXMMA"):
                speaker = "mma"
            else:
                speaker = "other"
        speaker_buckets.setdefault(speaker, []).append(c)

    # Build output ordered by PERFORMANCE_SPEAKER_ORDER (then alphabetical tail)
    seen: set[str] = set()
    ordered_speakers: list[str] = []
    for spk in PERFORMANCE_SPEAKER_ORDER:
        if spk in speaker_buckets:
            ordered_speakers.append(spk)
            seen.add(spk)
    for spk in sorted(speaker_buckets):
        if spk not in seen:
            ordered_speakers.append(spk)

    out = []
    for spk in ordered_speakers:
        cards = speaker_buckets[spk]
        meta = _INTEL_CATEGORY_META.get(spk, {"label": spk.title(), "icon": "📊"})

        phrases_out = []
        for c in sorted(cards, key=lambda x: (x.get("side") != "BUY_YES", x.get("side") != "BUY_NO")):
            phrase = (c.get("phrase") or "").lower().strip()
            assess = assess_by_phrase.get(phrase, {})
            raw_codes = []
            try:
                raw_codes = _json.loads(c.get("raw_json") or "{}").get("reason_codes", [])
            except Exception:
                pass
            score_conf = next(
                (r for r in raw_codes if r.startswith("SCORE_CONF_")), ""
            )
            phrases_out.append({
                "phrase":           c.get("phrase", ""),
                "market_id":        c.get("market_id", ""),
                "side":             c.get("side", "WATCH"),
                "yes_ask":          c.get("yes_ask"),
                "no_ask":           c.get("no_ask"),
                "ev_yes":           c.get("ev_yes"),
                "ev_no":            c.get("ev_no"),
                "p_literal":        c.get("p_literal"),
                "score_conf":       score_conf.replace("SCORE_CONF_", "") if score_conf else "",
                "llm_boost":        assess.get("boost"),
                "llm_confidence":   assess.get("confidence", ""),
                "llm_reasoning":    assess.get("reasoning", ""),
                "llm_evidence":     assess.get("evidence", ""),
                "llm_direct":       bool(assess.get("direct_evidence")),
                "llm_topic":        assess.get("topic", ""),
            })

        boosted    = sum(1 for p in phrases_out if (p["llm_boost"] or 1.0) > 1.05)
        suppressed = sum(1 for p in phrases_out if (p["llm_boost"] or 1.0) < 0.95)
        buy_count  = sum(1 for p in phrases_out if p["side"] in ("BUY_YES", "BUY_NO"))

        out.append({
            "category":   spk,
            "label":      meta["label"],
            "icon":       meta["icon"],
            "total":      len(phrases_out),
            "buy_count":  buy_count,
            "boosted":    boosted,
            "suppressed": suppressed,
            "phrases":    phrases_out,
        })

    return out


def _normalize_phrase_trend_speaker(raw: str | None) -> str:
    s = (raw or "auto").lower().strip() or "auto"
    return _PHRASE_TREND_SPEAKER_ALIASES.get(s, s)


def _query_phrase_trends() -> dict:
    """Load phrase_trends.json; global summary + per-speaker slices for Performance tab."""
    trends_path = Path("data/phrase_trends.json")
    if not trends_path.exists():
        return {
            "computed_at": None,
            "total_phrases": 0,
            "trending_up": [],
            "trending_down": [],
            "by_speaker": {spk: {"up": [], "down": [], "movers": []} for spk in PERFORMANCE_SPEAKER_ORDER},
        }
    try:
        data = json.loads(trends_path.read_text(encoding="utf-8"))
        raw: dict = data.get("trends", {})
        up_all = [v for v in raw.values() if v.get("trend_flag") == "VOCAB_TRENDING_UP"]
        down_all = [v for v in raw.values() if v.get("trend_flag") == "VOCAB_TRENDING_DOWN"]
        up_all.sort(key=lambda x: -(x.get("trend_delta") or 0))
        down_all.sort(key=lambda x: (x.get("trend_delta") or 0))

        by_spk: dict[str, dict] = {
            spk: {"up": [], "down": [], "movers": []} for spk in PERFORMANCE_SPEAKER_ORDER
        }

        for v in raw.values():
            spk = _normalize_phrase_trend_speaker(v.get("speaker"))
            if spk not in by_spk:
                by_spk[spk] = {"up": [], "down": [], "movers": []}
            flag = v.get("trend_flag")
            if flag == "VOCAB_TRENDING_UP":
                by_spk[spk]["up"].append(v)
            elif flag == "VOCAB_TRENDING_DOWN":
                by_spk[spk]["down"].append(v)
            td = v.get("trend_delta")
            if td is not None and v.get("rates", {}).get("30d") is not None and v.get("rates", {}).get("90d") is not None:
                by_spk[spk]["movers"].append(v)

        for spk, buckets in by_spk.items():
            buckets["up"].sort(key=lambda x: -(x.get("trend_delta") or 0))
            buckets["down"].sort(key=lambda x: (x.get("trend_delta") or 0))
            buckets["movers"].sort(
                key=lambda x: -abs(x.get("trend_delta") or 0),
            )
            buckets["up"] = buckets["up"][:12]
            buckets["down"] = buckets["down"][:12]
            buckets["movers"] = buckets["movers"][:15]

        return {
            "computed_at": data.get("computed_at"),
            "total_phrases": data.get("total_phrases", 0),
            "trending_up": up_all[:20],
            "trending_down": down_all[:20],
            "by_speaker": by_spk,
        }
    except Exception:
        return {
            "computed_at": None,
            "total_phrases": 0,
            "trending_up": [],
            "trending_down": [],
            "by_speaker": {spk: {"up": [], "down": [], "movers": []} for spk in PERFORMANCE_SPEAKER_ORDER},
        }


def _query_signals_intel() -> dict:
    """Load signal pipeline state: Truth Social posts, signal context freshness, drift alerts."""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    result: dict = {}

    # ── Truth Social posts ────────────────────────────────────────────────────
    ts_path = Path("data/truth_social_posts.json")
    ts_posts = []
    if ts_path.exists():
        try:
            raw = json.loads(ts_path.read_text(encoding="utf-8"))
            posts = raw.get("posts", []) if isinstance(raw, dict) else raw
            for p in sorted(posts, key=lambda x: x.get("posted_at", ""), reverse=True):
                raw_ts = p.get("posted_at") or p.get("created_at") or ""
                age_h = None
                if raw_ts:
                    try:
                        dt = _dt.datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                        age_h = (now - dt).total_seconds() / 3600
                    except Exception:
                        pass
                # Calculate boost tier
                boost = 1.02
                if age_h is not None:
                    for max_h, mult in [(2, 1.50), (6, 1.35), (12, 1.20), (24, 1.10), (72, 1.07), (168, 1.05)]:
                        if age_h <= max_h:
                            boost = mult
                            break
                ts_posts.append({
                    "id": p.get("id", ""),
                    "content": (p.get("content") or "")[:300],
                    "posted_at": raw_ts,
                    "url": p.get("url", ""),
                    "age_h": round(age_h, 1) if age_h is not None else None,
                    "boost": boost,
                })
        except Exception:
            pass
    result["truth_social"] = ts_posts

    # ── Signal context freshness ──────────────────────────────────────────────
    ctx_path = Path("data/signal_context.json")
    ctx_meta: dict = {"exists": False}
    if ctx_path.exists():
        try:
            ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
            fetched_at = ctx.get("fetched_at", "")
            stats = ctx.get("stats", {})
            sources = ctx.get("sources", {})
            age_min = None
            if fetched_at:
                try:
                    dt = _dt.datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
                    age_min = round((now - dt).total_seconds() / 60, 0)
                except Exception:
                    pass
            ctx_meta = {
                "exists": True,
                "fetched_at": fetched_at,
                "age_min": age_min,
                "source_counts": {k: len(v) if isinstance(v, list) else 0 for k, v in sources.items()},
                "phrase_count": len(ctx.get("phrase_universe", [])),
            }
        except Exception:
            ctx_meta = {"exists": True, "error": "parse error"}
    result["signal_context"] = ctx_meta

    # ── LLM analysis freshness ────────────────────────────────────────────────
    llm_path = Path("data/llm_analysis.json")
    llm_meta: dict = {"exists": False}
    if llm_path.exists():
        try:
            llm = json.loads(llm_path.read_text(encoding="utf-8"))
            analyzed_at = llm.get("analyzed_at", "")
            age_min = None
            if analyzed_at:
                try:
                    dt = _dt.datetime.fromisoformat(analyzed_at.replace("Z", "+00:00"))
                    age_min = round((now - dt).total_seconds() / 60, 0)
                except Exception:
                    pass
            llm_meta = {
                "exists": True,
                "analyzed_at": analyzed_at,
                "age_min": age_min,
                "topics": len(llm.get("topics", [])),
                "boosts": len([a for a in llm.get("assessments", []) if a.get("boost", 1) > 1.0]),
                "suppresses": len([a for a in llm.get("assessments", []) if a.get("boost", 1) < 1.0]),
                "model": llm.get("model", ""),
            }
        except Exception:
            llm_meta = {"exists": True, "error": "parse error"}
    result["llm_analysis"] = llm_meta

    # ── Drift alerts ──────────────────────────────────────────────────────────
    drift_path = Path("data/drift_alerts.json")
    drift_alerts = []
    if drift_path.exists():
        try:
            drift_raw = json.loads(drift_path.read_text(encoding="utf-8"))
            drift_alerts = drift_raw if isinstance(drift_raw, list) else drift_raw.get("alerts", [])
        except Exception:
            pass
    result["drift_alerts"] = drift_alerts

    # ── WH schedule freshness + recent events ─────────────────────────────────
    wh_path = Path("data/wh_schedule.json")
    wh_meta: dict = {"exists": False}
    if wh_path.exists():
        try:
            import os as _os
            mtime = _os.path.getmtime(str(wh_path))
            age_min = round((now.timestamp() - mtime) / 60, 0)
            wh_data = json.loads(wh_path.read_text(encoding="utf-8"))
            events = wh_data if isinstance(wh_data, list) else wh_data.get("events", [])
            recent = []
            for ev in events[:20]:
                recent.append({
                    "title": (ev.get("title") or "")[:120],
                    "event_type": ev.get("event_type", ""),
                    "published_at": ev.get("published_at", ""),
                    "source": ev.get("source", ""),
                })
            wh_meta = {"exists": True, "age_min": age_min, "event_count": len(events), "events": recent}
        except Exception:
            wh_meta = {"exists": True, "error": "parse error"}
    result["wh_schedule"] = wh_meta

    # ── signals.yaml (LLM boost file) freshness ───────────────────────────────
    sig_path = Path("data/signals.yaml")
    sig_meta: dict = {"exists": False}
    if sig_path.exists():
        try:
            import os as _os
            import yaml as _yaml
            mtime = _os.path.getmtime(str(sig_path))
            age_min = round((now.timestamp() - mtime) / 60, 0)
            raw_sig = _yaml.safe_load(sig_path.read_text(encoding="utf-8")) or {}
            sigs = raw_sig.get("signals", []) if isinstance(raw_sig, dict) else []
            boosted   = sum(1 for s in sigs if s.get("llm_boost", 1.0) > 1.05)
            suppressed = sum(1 for s in sigs if s.get("llm_boost", 1.0) < 0.95)
            high_conf  = sum(1 for s in sigs if s.get("llm_confidence") == "high")
            direct_ev  = sum(1 for s in sigs if s.get("llm_direct_ev"))
            stale = age_min is not None and age_min > 60
            sig_meta = {
                "exists":    True,
                "age_min":   age_min,
                "stale":     stale,
                "total":     len(sigs),
                "boosted":   boosted,
                "suppressed": suppressed,
                "high_conf": high_conf,
                "direct_ev": direct_ev,
            }
        except Exception:
            sig_meta = {"exists": True, "error": "parse error"}
    result["signals_yaml"] = sig_meta

    return result


def _query_calibration_buckets() -> dict:
    """Compute live calibration diagnostics from outcome_reviews.

    Returns per p_literal bucket: N, actual YES rate, and calibrated model rate.
    Used by the Intelligence tab Model Health card.
    """
    db_path = Path("data/edge.db")
    if not db_path.exists():
        return {"error": "no db", "buckets": []}
    try:
        from app.db import connect as _db_connect
        conn = _db_connect(db_path)
        rows = conn.execute(
            "SELECT p_literal, outcome FROM outcome_reviews "
            "WHERE outcome IN ('yes','no') AND p_literal IS NOT NULL "
            "ORDER BY prediction_ts DESC LIMIT 500"
        ).fetchall()
        conn.close()
    except Exception as exc:
        return {"error": str(exc), "buckets": []}

    if not rows:
        return {"buckets": [], "total": 0}

    BUCKET_DEFS = [
        (0.00, 0.05,  "0–5%"),
        (0.05, 0.10,  "5–10%"),
        (0.10, 0.15,  "10–15%"),
        (0.15, 0.20,  "15–20%"),
        (0.20, 0.30,  "20–30%"),
        (0.30, 0.50,  "30–50%"),
        (0.50, 1.01,  "50%+"),
    ]
    buckets = []
    for lo, hi, label in BUCKET_DEFS:
        subset = [(float(r[0]), 1 if r[1] == "yes" else 0) for r in rows if lo <= float(r[0]) < hi]
        if not subset:
            buckets.append({"label": label, "n": 0, "actual_pct": None, "model_pct": round((lo + hi) / 2 * 100, 1)})
            continue
        ps, ys = zip(*subset)
        actual_rate = sum(ys) / len(ys)
        model_rate  = sum(ps) / len(ps)
        gap_pp = (actual_rate - model_rate) * 100
        buckets.append({
            "label":      label,
            "n":          len(subset),
            "actual_pct": round(actual_rate * 100, 1),
            "model_pct":  round(model_rate * 100, 1),
            "gap_pp":     round(gap_pp, 1),
        })

    total_yes = sum(1 for r in rows if r[1] == "yes")
    return {
        "buckets": buckets,
        "total":   len(rows),
        "overall_yes_pct": round(total_yes / len(rows) * 100, 1) if rows else None,
    }


class DashboardHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", ""):
            self._serve_html()
        elif parsed.path == "/api/data":
            self._serve_api()
        elif parsed.path == "/api/script-output":
            self._serve_script_output(parsed)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/run-script":
            self._handle_run_script()
        else:
            self.send_error(404)

    def _handle_run_script(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            script = body.get("script", "").strip()
            script_id = body.get("id", "").strip()
            if not script or script not in _ALLOWED_SCRIPTS:
                self._json({"error": f"Script not allowed: {script!r}"}, status=400)
                return
            job_id = str(uuid.uuid4())
            _SCRIPT_JOBS[job_id] = {
                "job_id": job_id,
                "script": script,
                "script_id": script_id,
                "status": "running",
                "output": [],
                "exit_code": None,
                "pid": None,
                "started_at": time.time(),
                "ended_at": None,
            }
            t = threading.Thread(
                target=_run_script_job,
                args=(job_id, script),
                daemon=True,
            )
            t.start()
            self._json({"job_id": job_id, "status": "running"})
        except Exception as exc:
            self._json({"error": str(exc)}, status=500)

    def _serve_script_output(self, parsed):
        qs = parse_qs(parsed.query)
        job_id = (qs.get("job") or [""])[0]
        job = _SCRIPT_JOBS.get(job_id)
        if job is None:
            self._json({"error": "unknown job"}, status=404)
            return
        elapsed = None
        if job.get("started_at"):
            end = job.get("ended_at") or time.time()
            elapsed = round(end - job["started_at"], 2)
        self._json({
            "job_id": job_id,
            "status": job["status"],
            "output": list(job["output"]),
            "exit_code": job["exit_code"],
            "elapsed_sec": elapsed,
        })

    def _json(self, data: dict, status: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _serve_html(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(HTML_TEMPLATE.encode("utf-8"))

    def _serve_api(self):
        import time as _time
        import traceback as _tb
        cached = _API_CACHE.get("ts")
        if (
            cached
            and _API_CACHE.get("ver") == _API_SCHEMA_VERSION
            and (_time.monotonic() - cached) < _API_CACHE_TTL
        ):
            body = _API_CACHE["body"]
        else:
            errors: list[str] = []

            def _safe(label: str, fn, default=None):
                """Run fn(); on failure log and return default."""
                try:
                    return fn()
                except Exception as exc:
                    errors.append(f"{label}: {exc}")
                    logger.warning("API section '%s' failed: %s", label, _tb.format_exc())
                    return default

            conn = _get_conn()
            try:
                market_cache = _safe("market_cache", _load_market_cache, {})
                outcome_rates = _safe("outcome_rates", _load_outcome_rates, {})
                confirmed_phrases = _safe("confirmed_phrases", lambda: _query_confirmed_phrases(conn), set())

                all_cards = _safe("all_cards", lambda: _query_all_cards(
                    conn, market_cache, outcome_rates, confirmed_phrases,
                ), [])

                def _sport_cards(like: str) -> list[dict]:
                    return _query_all_cards(
                        conn, market_cache, outcome_rates, confirmed_phrases,
                        market_id_like=like, apply_blocked_filter=False,
                    )
                nba_cards   = _safe("nba_cards", lambda: _sport_cards("KXNBAMENTION%"), [])
                ncaab_cards = _safe("ncaab_cards", lambda: _sport_cards("KXNCAABMENTION%"), [])
                mlb_cards   = _safe("mlb_cards", lambda: _sport_cards("KXMLBMENTION%"), [])
                fight_cards = _safe("fight_cards", lambda: _sport_cards("KXFIGHT%"), [])
                wbc_cards   = _safe("wbc_cards", lambda: _sport_cards("KXWBC%"), [])

                event_meta = _safe("event_meta", lambda: _merge_event_meta_from_cards(_load_event_meta(), all_cards), {})
                groups = _safe("groups", lambda: _build_groups(all_cards, event_meta), [])
                coverage = _safe("coverage", lambda: _query_coverage(conn, market_cache, all_cards), {})

                llm_analysis: dict = {}
                if LLM_ANALYSIS_CACHE.exists():
                    try:
                        llm_analysis = json.loads(LLM_ANALYSIS_CACHE.read_text(encoding="utf-8"))
                    except Exception:
                        pass
                llm_analysis["_signal_model"] = os.getenv("LLM_SIGNAL_MODEL", "gpt-4o-mini")
                llm_analysis["_event_model"] = os.getenv("LLM_EVENT_MODEL", "gpt-4o-mini")

                payload = {
                    "status": _safe("status", lambda: _query_status(conn, coverage=coverage), {}),
                    "groups": groups,
                    "sports": _safe("sports", lambda: _query_sports(
                        conn, nba_cards, ncaab_cards, mlb_cards, fight_cards, wbc_cards,
                    ), {}),
                    "coverage": coverage,
                    "all_cards": all_cards,
                    "health": _safe("health", lambda: _query_health(conn), {}),
                    "llm_analysis": llm_analysis,
                    "outcomes": _safe("outcomes", lambda: _query_outcomes(conn), {}),
                    "phrase_trends": _safe("phrase_trends", _query_phrase_trends, {}),
                    "signals_intel": _safe("signals_intel", _query_signals_intel, {}),
                    "intelligence": _safe("intelligence", lambda: _query_intelligence(all_cards, llm_analysis), []),
                    "calib_buckets": _safe("calib_buckets", _query_calibration_buckets, {}),
                }
                if errors:
                    payload["_api_errors"] = errors
            finally:
                conn.close()

            body = json.dumps(payload, default=str).encode("utf-8")
            _API_CACHE["body"] = body
            _API_CACHE["ts"] = _time.monotonic()
            _API_CACHE["ver"] = _API_SCHEMA_VERSION

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="Kalshi Mention Edge Dashboard")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open browser")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"Database not found at {DB_PATH}. Run the engine first.")
        raise SystemExit(1)

    HTTPServer.allow_reuse_address = True
    server = HTTPServer(("127.0.0.1", args.port), DashboardHandler)
    url = f"http://localhost:{args.port}"
    print(f"Dashboard running at {url}")
    print("Press Ctrl+C to stop.")

    if not args.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
