#!/usr/bin/env python3
"""Automated Rev transcript discovery → cleaning → corpus ingestion.

Runs the full pipeline for every configured speaker end-to-end:
  1. Discover new Rev transcript URLs via discover_rev_transcripts
  2. For each new URL: fetch, extract target speaker's text, write corpus file
  3. Persist results to the ingestion ledger (data/corpus/.rev_ingested.json)
  4. Run ingest_corpus so the DB + phrase_hits are updated immediately

Designed to be called by MaintenanceRunner once per day (86400s interval).
Also callable manually for an immediate refresh.

Usage
-----
  # Normal run — all configured speakers:
  python3 scripts/auto_ingest_corpus.py

  # Dry-run: discover and report without writing any files:
  python3 scripts/auto_ingest_corpus.py --dry-run

  # Force-ingest a specific URL regardless of ledger status:
  python3 scripts/auto_ingest_corpus.py --force-url "https://www.rev.com/..."

  # Run only one speaker:
  python3 scripts/auto_ingest_corpus.py --only trump

  # Skip the final ingest_corpus DB step (file writing only):
  python3 scripts/auto_ingest_corpus.py --no-db-ingest

Environment variables
---------------------
  BRAVE_API_KEY          — enables Brave Search for URL discovery (recommended)
  REV_SPEAKERS           — comma-separated speaker slugs to run, default: trump,leavitt,mamdani
  REV_DISCOVER_MAX       — max URLs to discover per speaker per run, default: 10

  Per-speaker overrides (SPEAKER = uppercased slug, e.g. TRUMP, LEAVITT, MAMDANI):
  REV_<SPEAKER>_KEYWORD  — search keyword override
  REV_<SPEAKER>_LABELS   — comma-separated speaker label overrides

  Legacy single-speaker vars (still respected):
  REV_CORPUS_SPEAKER, REV_TARGET_SPEAKERS, REV_DISCOVER_KEYWORD
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import traceback
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.discover_rev_transcripts import discover, load_ledger
from scripts.rev_transcript_cleaner import (
    _DEFAULT_TRUMP_SPEAKERS,
    fetch_html,
    process_source,
)

# ── Constants ──────────────────────────────────────────────────────────────────
LEDGER_PATH = Path("data/corpus/.rev_ingested.json")

_DEFAULT_DISCOVER_MAX = 10

# Built-in speaker profiles — these are the defaults when no env overrides are set.
# keyword   : passed to Brave / Rev search
# labels    : speaker name variants to extract from the transcript (case-insensitive)
_SPEAKER_DEFAULTS: dict[str, dict] = {
    "trump": {
        "keyword": "trump",
        "labels": ["donald trump", "president trump", "president donald trump", "trump"],
    },
    "leavitt": {
        "keyword": "karoline leavitt",
        "labels": ["karoline leavitt", "press secretary leavitt", "leavitt"],
    },
    "mamdani": {
        "keyword": "zohran mamdani",
        "labels": ["zohran mamdani", "mayor mamdani", "mayor zohran mamdani", "mamdani"],
    },
    # KXFEDMENTION — Fed chair press conferences (structured, formulaic language)
    "powell": {
        "keyword": "jerome powell press conference",
        "labels": [
            "jerome powell", "chair powell", "chairman powell", "powell",
            "federal reserve chair", "fed chair",
        ],
    },
    # KXCARNEYMENTION — Canadian PM (tariff / trade war focus)
    "carney": {
        "keyword": "mark carney speech",
        "labels": [
            "mark carney", "prime minister carney", "pm carney", "carney",
            "mr. carney",
        ],
    },
    # KXSTARMERMENTIONB — UK PM (PMQs / parliament / press conferences)
    "starmer": {
        "keyword": "keir starmer speech",
        "labels": [
            "keir starmer", "prime minister starmer", "pm starmer", "starmer",
            "sir keir starmer", "sir keir",
        ],
    },
    # KXHOMANMENTION — Border czar (enforcement speeches / TV appearances)
    "homan": {
        "keyword": "tom homan speech",
        "labels": [
            "tom homan", "thomas homan", "border czar homan", "homan",
            "mr. homan",
        ],
    },
}


def _speaker_config(slug: str) -> dict:
    """Return keyword + labels for a speaker slug, respecting env overrides."""
    defaults = _SPEAKER_DEFAULTS.get(slug, {"keyword": slug, "labels": [slug]})
    key_prefix = f"REV_{slug.upper()}_"
    keyword = os.getenv(f"{key_prefix}KEYWORD", defaults["keyword"])
    labels_raw = os.getenv(f"{key_prefix}LABELS", "")
    labels = (
        [l.strip().lower() for l in labels_raw.split(",") if l.strip()]
        if labels_raw
        else defaults["labels"]
    )
    return {"keyword": keyword, "labels": labels}


def _active_speakers() -> list[str]:
    """Return the list of speaker slugs to process this run."""
    raw = os.getenv("REV_SPEAKERS", "trump,leavitt,mamdani,powell,carney,starmer,homan")
    return [s.strip().lower() for s in raw.split(",") if s.strip()]


# ── Ledger I/O ─────────────────────────────────────────────────────────────────
def _save_ledger(ledger: dict) -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEDGER_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(LEDGER_PATH)


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Core pipeline ──────────────────────────────────────────────────────────────
def run_pipeline(
    *,
    corpus_speaker: str,
    target_speakers: list[str],
    discover_keyword: str,
    discover_max: int,
    force_url: str | None,
    dry_run: bool,
    skip_db_ingest: bool,
    verbose: bool,
) -> dict:
    """Execute discover → clean → ingest. Returns a run-summary dict."""
    ledger = load_ledger()
    summary = {
        "run_at": _now_iso(),
        "discovered": 0,
        "new": 0,
        "ingested": 0,
        "skipped": 0,
        "failed": 0,
        "corpus_files_written": [],
        "errors": [],
    }

    # ── Step 1: gather URLs ────────────────────────────────────────────────────
    if force_url:
        urls = [force_url.strip()]
        _log(f"Force-processing: {force_url}", verbose)
    else:
        _log(f"Discovering new Rev URLs (keyword='{discover_keyword}', max={discover_max})…", verbose)
        urls = discover(
            keyword=discover_keyword,
            max_results=discover_max,
            show_all=False,
            verbose=verbose,
        )
        summary["discovered"] = len(urls)
        if not urls:
            _log("No new URLs found — corpus is up to date.", verbose=True)
            return summary
        _log(f"Found {len(urls)} new URL(s):", verbose=True)
        for u in urls:
            _log(f"  {u}", verbose=True)

    # ── Step 2: process each URL ───────────────────────────────────────────────
    files_written: list[str] = []

    for url in urls:
        entry = ledger.get(url, {})
        # Skip already-OK entries unless --force-url
        if not force_url and entry.get("status") in ("ok", "skipped"):
            summary["skipped"] += 1
            continue

        summary["new"] += 1

        if dry_run:
            _log(f"  [dry-run] Would process: {url}", verbose=True)
            continue

        _log(f"  Fetching: {url}", verbose=True)
        try:
            html = fetch_html(url, timeout=25)
        except SystemExit as exc:
            msg = str(exc)
            _log(f"  ERROR fetching {url}: {msg}", verbose=True)
            summary["failed"] += 1
            summary["errors"].append({"url": url, "stage": "fetch", "error": msg})
            ledger[url] = {
                "first_seen_at": entry.get("first_seen_at", _now_iso()),
                "processed_at": _now_iso(),
                "status": "error",
                "error": msg,
            }
            _save_ledger(ledger)
            continue
        except Exception as exc:
            msg = traceback.format_exc()
            summary["failed"] += 1
            summary["errors"].append({"url": url, "stage": "fetch", "error": str(exc)})
            ledger[url] = {
                "first_seen_at": entry.get("first_seen_at", _now_iso()),
                "processed_at": _now_iso(),
                "status": "error",
                "error": str(exc),
            }
            _save_ledger(ledger)
            continue

        # Capture what file was written by redirecting stdout temporarily
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                ok = process_source(
                    html=html,
                    url=url,
                    target_speakers=target_speakers,
                    corpus_speaker=corpus_speaker,
                    event_type_override=None,
                    date_override=None,
                    stdout_mode=False,
                )
        except Exception as exc:
            ok = False
            summary["failed"] += 1
            summary["errors"].append({"url": url, "stage": "clean", "error": str(exc)})
            ledger[url] = {
                "first_seen_at": entry.get("first_seen_at", _now_iso()),
                "processed_at": _now_iso(),
                "status": "error",
                "error": str(exc),
            }
            _save_ledger(ledger)
            _log(f"  ERROR cleaning {url}: {exc}", verbose=True)
            continue

        # Extract the output path from captured stdout
        out_path: str | None = None
        for line in buf.getvalue().splitlines():
            if "Saved to" in line:
                out_path = line.split(":", 1)[-1].strip()
                break

        # Also print the summary to real stdout
        captured = buf.getvalue().strip()
        if captured:
            print(captured)

        if ok and out_path:
            summary["ingested"] += 1
            files_written.append(out_path)
            summary["corpus_files_written"].append(out_path)
            ledger[url] = {
                "first_seen_at": entry.get("first_seen_at", _now_iso()),
                "processed_at": _now_iso(),
                "status": "ok",
                "output_file": out_path,
            }
            _log(f"  → {out_path}", verbose=True)
        else:
            # process_source returned False = no target speaker text found
            summary["skipped"] += 1
            ledger[url] = {
                "first_seen_at": entry.get("first_seen_at", _now_iso()),
                "processed_at": _now_iso(),
                "status": "skipped",
                "reason": "no_target_speaker_content",
            }
            _log(f"  Skipped (no target speaker text): {url}", verbose=True)

        _save_ledger(ledger)

    # ── Step 3: DB ingestion ───────────────────────────────────────────────────
    if files_written and not skip_db_ingest:
        _log(f"\nRunning ingest_corpus for {len(files_written)} new file(s)…", verbose=True)
        _run_ingest_corpus()
    elif files_written and skip_db_ingest:
        _log("Skipping DB ingest (--no-db-ingest).", verbose=True)

    return summary


def _run_ingest_corpus() -> None:
    """Run ingest_corpus in-process (avoids subprocess overhead)."""
    try:
        from scripts.ingest_corpus import ingest_corpus, _build_matcher
        from app.db import init_db

        db_path = Path("data/edge.db")
        conn = init_db(db_path)
        matcher = _build_matcher()
        ingest_corpus(conn, matcher, fresh=False)
        conn.close()
    except Exception as exc:
        print(f"WARNING: ingest_corpus step failed: {exc}", file=sys.stderr)


def _log(msg: str, verbose: bool = False) -> None:
    if verbose:
        print(msg)


# ── CLI ────────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Auto-discover, clean, and ingest Rev transcripts for all speakers.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover URLs and report; do not write any files.",
    )
    p.add_argument(
        "--force-url",
        metavar="URL",
        help="Force-process this URL (bypasses ledger). Use with --only to specify speaker.",
    )
    p.add_argument(
        "--only",
        metavar="SLUG",
        help="Run only this speaker slug (e.g. trump, leavitt, mamdani).",
    )
    p.add_argument(
        "--max",
        type=int,
        default=int(os.getenv("REV_DISCOVER_MAX", str(_DEFAULT_DISCOVER_MAX))),
        metavar="N",
        help="Max URLs to discover per speaker per run (default: 10).",
    )
    p.add_argument(
        "--no-db-ingest",
        action="store_true",
        help="Write corpus files but skip the ingest_corpus DB step.",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Print progress details.",
    )
    args = p.parse_args(argv)

    speakers = _active_speakers()
    if args.only:
        speakers = [args.only.strip().lower()]

    total = {"discovered": 0, "ingested": 0, "skipped": 0, "failed": 0, "errors": []}
    any_files_written = False

    for slug in speakers:
        cfg = _speaker_config(slug)
        if args.verbose or args.dry_run:
            print(f"\n── Speaker: {slug} (keyword={cfg['keyword']!r}) ──")

        summary = run_pipeline(
            corpus_speaker=slug,
            target_speakers=cfg["labels"],
            discover_keyword=cfg["keyword"],
            discover_max=args.max,
            force_url=args.force_url,
            dry_run=args.dry_run,
            skip_db_ingest=True,  # we run ingest once at the end
            verbose=args.verbose or args.dry_run,
        )

        for k in ("discovered", "ingested", "skipped", "failed"):
            total[k] += summary[k]
        total["errors"].extend(summary.get("errors", []))
        if summary["ingested"] > 0:
            any_files_written = True

    # Run ingest_corpus once after all speakers are processed
    if any_files_written and not args.no_db_ingest:
        if args.verbose:
            print(f"\nRunning ingest_corpus ({total['ingested']} new file(s))…")
        try:
            from scripts.ingest_corpus import ingest_corpus, _build_matcher
            from app.db import init_db
            conn = init_db(Path("data/edge.db"))
            matcher = _build_matcher()
            ingest_corpus(conn, matcher, fresh=False)
            conn.close()
        except Exception as exc:
            print(f"WARNING: ingest_corpus step failed: {exc}", file=sys.stderr)

    # One-line summary for MaintenanceRunner logs
    print(
        f"auto_ingest_corpus: speakers={len(speakers)} "
        f"discovered={total['discovered']} "
        f"ingested={total['ingested']} "
        f"skipped={total['skipped']} "
        f"failed={total['failed']}"
    )

    if total["errors"]:
        for e in total["errors"]:
            print(f"  ERROR [{e['stage']}] {e['url']}: {e['error']}", file=sys.stderr)

    return 1 if total["failed"] > 0 and total["ingested"] == 0 else 0


if __name__ == "__main__":
    sys.exit(main())
