#!/usr/bin/env python3
"""Add a Truth Social post to data/truth_social_posts.json.

Usage examples:

1. Pass post text as a positional argument:
   python3 scripts/add_truth_social_post.py "We are going to DRILL BABY DRILL!"

2. Pipe from stdin (paste and hit Ctrl+D):
   python3 scripts/add_truth_social_post.py

3. Supply the real post time with --posted-at (full ISO 8601):
   python3 scripts/add_truth_social_post.py "DRILL BABY DRILL!" \\
       --posted-at "2026-03-23T09:42:00-04:00"

4. Supply post time in Eastern time with --eastern (no timezone math needed):
   python3 scripts/add_truth_social_post.py "DRILL BABY DRILL!" \\
       --eastern "9:42am"           # assumes today's date
   python3 scripts/add_truth_social_post.py "DRILL BABY DRILL!" \\
       --eastern "2026-03-23 9:42"  # explicit date
   Accepts: "9:42am", "9:42 AM", "09:42", "2026-03-23 9:42", etc.
   Observes US Eastern DST (ET = UTC-5 in winter, UTC-4 in summer).

5. Store the Truth Social post URL:
   python3 scripts/add_truth_social_post.py "DRILL BABY DRILL!" \\
       --url "https://truthsocial.com/@realDonaldTrump/posts/123456"

6. Auto-apply signals immediately (skips waiting for MaintenanceRunner):
   python3 scripts/add_truth_social_post.py "DRILL BABY DRILL!" \\
       --eastern "9:42am" --process

7. Fix a previously ingested post's timestamp:
   python3 scripts/add_truth_social_post.py "DRILL BABY DRILL!" \\
       --update-timestamp --eastern "9:42am"

If --posted-at and --eastern are both omitted the timestamp defaults to NOW
(UTC ingestion time).  Accurate timestamps improve time-decay boost quality:
a post 2h old gets 1.50x; a post 6h old gets 1.35x.

Retention: posts older than 7 days (168 hours) are pruned on each run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

POSTS_PATH  = Path(__file__).parent.parent / "data" / "truth_social_posts.json"
PROCESS_SCRIPT = Path(__file__).parent / "process_truth_social.py"
MAX_AGE_HOURS = 168  # 7 days — weekly markets need this history

# Time-decay windows (mirrors process_truth_social.py DECAY_WINDOWS)
_DECAY_WINDOWS = [
    (2,   1.50, "< 2h  — direct pre-speech signal  ★★★"),
    (6,   1.35, "2–6h  — same-day signal            ★★"),
    (12,  1.20, "6–12h — warm signal                ★★"),
    (24,  1.10, "12–24h — mild signal               ★"),
    (72,  1.07, "1–3 days — weekly market context"),
    (168, 1.05, "3–7 days — background context"),
]
_DECAY_DEFAULT = (1.02, "> 7 days — long tail only")


def _load() -> dict:
    if POSTS_PATH.exists():
        try:
            return json.loads(POSTS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"fetched_at": "", "posts": []}


def _save(data: dict) -> None:
    POSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    POSTS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _prune_old(posts: list[dict]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)
    kept = []
    for p in posts:
        try:
            dt = datetime.fromisoformat(p["posted_at"].replace("Z", "+00:00"))
            if dt >= cutoff:
                kept.append(p)
        except Exception:
            kept.append(p)  # keep if can't parse
    return kept


def _decay_label(age_h: float) -> str:
    for max_age, mult, label in _DECAY_WINDOWS:
        if age_h <= max_age:
            return f"{mult:.2f}x  ({label})"
    return f"{_DECAY_DEFAULT[0]:.2f}x  ({_DECAY_DEFAULT[1]})"


def _parse_posted_at(raw: str) -> datetime:
    """Parse an ISO 8601 string into a UTC-aware datetime; raise ValueError on failure."""
    raw = raw.strip()
    # Allow bare dates like "2026-03-12" → treat as midnight UTC
    if len(raw) == 10:
        raw += "T00:00:00+00:00"
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_eastern(raw: str, now_utc: datetime) -> datetime:
    """Parse a time string in US Eastern time and return a UTC-aware datetime.

    Accepts:
      - "9:42am", "9:42 AM", "09:42"         → today's date in ET
      - "2026-03-23 9:42", "2026-03-23T09:42" → explicit date in ET

    Observes US Eastern DST:
      - EDT (UTC-4): second Sunday of March → first Sunday of November
      - EST (UTC-5): otherwise
    """
    raw = raw.strip()

    # Determine ET offset by checking if DST is active on the target date.
    # We'll resolve the date first (using today as default), then check DST.
    import re

    # Try to extract a date portion.
    # Supported inputs:
    #   - "2026-03-23 9:42" / "2026-03-23T09:42"
    #   - "Mar 20 · 6:15 PM" / "Mar 20 6:15 PM" (assumes current year)
    #   - "9:42am" (assumes today's date)

    # 1) ISO date prefix
    date_match = re.match(r"(\d{4}-\d{2}-\d{2})[T ]?(.*)", raw)
    if date_match:
        date_str = date_match.group(1)
        time_str = date_match.group(2).strip()
    else:
        # 2) Truth Social style month/day prefix, e.g. "Mar 20 · 6:15 PM"
        cleaned = raw.replace("·", " ").replace("—", " ")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        md_match = re.match(r"^([A-Za-z]{3,9})\s+(\d{1,2})\s+(.*)$", cleaned)
        if md_match:
            mon = md_match.group(1)
            day = md_match.group(2)
            time_str = md_match.group(3).strip()
            year = now_utc.astimezone(timezone.utc).year
            try:
                month_num = datetime.strptime(mon[:3].title(), "%b").month
            except ValueError as exc:
                raise ValueError(f"Cannot parse month '{mon}'") from exc
            date_str = f"{year:04d}-{month_num:02d}-{int(day):02d}"
        else:
            # 3) No date — use today in ET (approximate: use UTC date as close enough)
            today = now_utc.date()
            date_str = today.isoformat()
            time_str = raw

    # Parse time component
    time_str = time_str.strip()
    if not time_str:
        time_str = "00:00"

    # Normalise AM/PM
    time_str = time_str.upper().replace(" AM", "AM").replace(" PM", "PM")

    # Try various time formats
    parsed_time = None
    for fmt in ("%I:%M%p", "%I%p", "%H:%M", "%H:%M:%S"):
        try:
            parsed_time = datetime.strptime(time_str, fmt).time()
            break
        except ValueError:
            continue

    if parsed_time is None:
        raise ValueError(
            f"Cannot parse time '{time_str}'. "
            "Expected formats: '9:42am', '9:42 AM', '09:42', '9:42PM'"
        )

    try:
        naive_dt = datetime.strptime(f"{date_str} {parsed_time.strftime('%H:%M:%S')}", "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError(f"Cannot parse date '{date_str}': {exc}") from exc

    # Determine ET offset: EDT=UTC-4 from 2nd Sun Mar → 1st Sun Nov; else EST=UTC-5
    year = naive_dt.year
    # DST starts: 2nd Sunday of March at 2am local
    dst_start = _nth_weekday(year, 3, 6, 2)  # month=3, weekday=6(Sun), n=2
    # DST ends: 1st Sunday of November at 2am local
    dst_end = _nth_weekday(year, 11, 6, 1)

    if dst_start <= naive_dt.replace(hour=2) < dst_end:
        et_offset = timedelta(hours=-4)  # EDT
        tz_label = "EDT (UTC-4)"
    else:
        et_offset = timedelta(hours=-5)  # EST
        tz_label = "EST (UTC-5)"

    et_tz = timezone(et_offset)
    aware_dt = naive_dt.replace(tzinfo=et_tz)
    print(f"Interpreted as Eastern time: {naive_dt.strftime('%Y-%m-%d %H:%M')} {tz_label}")
    return aware_dt.astimezone(timezone.utc)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime:
    """Return the nth occurrence of weekday (0=Mon, 6=Sun) in the given month/year."""
    first = datetime(year, month, 1)
    # Days until the first occurrence of weekday
    delta = (weekday - first.weekday()) % 7
    first_occurrence = first + timedelta(days=delta)
    return first_occurrence + timedelta(weeks=n - 1)


def _run_process_script() -> None:
    print("Running process_truth_social.py to apply signals now...")
    try:
        result = subprocess.run(
            [sys.executable, str(PROCESS_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            # Print just the summary lines (skip verbose per-phrase output)
            for line in result.stdout.splitlines():
                if line.strip():
                    print(f"  {line}")
        else:
            print(f"  process_truth_social.py exited with code {result.returncode}")
            if result.stderr:
                print(f"  {result.stderr.strip()}")
    except Exception as exc:
        print(f"  Could not run process_truth_social.py: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add a Truth Social post to data/truth_social_posts.json.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            '  python3 scripts/add_truth_social_post.py "DRILL BABY DRILL!"\n'
            '  python3 scripts/add_truth_social_post.py "DRILL BABY DRILL!" --eastern "9:42am"\n'
            '  python3 scripts/add_truth_social_post.py "DRILL BABY DRILL!" --eastern "9:42am" --process\n'
            '  python3 scripts/add_truth_social_post.py "DRILL BABY DRILL!" --update-timestamp --eastern "9:42am"\n'
        ),
    )
    parser.add_argument(
        "content",
        nargs="?",
        default=None,
        help="Post text. If omitted, reads from stdin.",
    )
    parser.add_argument(
        "--posted-at",
        metavar="ISO8601",
        default=None,
        help=(
            "Real Truth Social publish time in ISO 8601, e.g. '2026-03-23T09:42:00-04:00'. "
            "Defaults to NOW (UTC) if not supplied."
        ),
    )
    parser.add_argument(
        "--eastern",
        metavar="TIME",
        default=None,
        help=(
            "Post time in US Eastern time — no timezone math needed. "
            "Accepts '9:42am', '9:42 AM', '09:42', '2026-03-23 9:42'. "
            "Assumes today's date if no date is given. "
            "Observes EDT/EST automatically."
        ),
    )
    parser.add_argument(
        "--url",
        metavar="URL",
        default="",
        help="Truth Social post URL (optional, stored for reference).",
    )
    parser.add_argument(
        "--process",
        action="store_true",
        help="Immediately run process_truth_social.py after adding (don't wait 15 min).",
    )
    parser.add_argument(
        "--update-timestamp",
        action="store_true",
        help=(
            "Update the posted_at of an existing post instead of skipping it as a duplicate. "
            "Useful when a post was added without --posted-at and needs the real time."
        ),
    )
    args = parser.parse_args()

    # --posted-at and --eastern are mutually exclusive
    if args.posted_at and args.eastern:
        print("Error: --posted-at and --eastern cannot be used together.")
        sys.exit(1)

    # Resolve content
    if args.content is not None:
        content = args.content.strip()
    else:
        print("Paste the Truth Social post below, then press Ctrl+D (Mac/Linux) or Ctrl+Z Enter (Windows):")
        content = sys.stdin.read().strip()

    if not content:
        print("No content provided — nothing written.")
        sys.exit(0)

    # Resolve posted_at timestamp
    now = datetime.now(timezone.utc)

    if args.posted_at:
        try:
            posted_at = _parse_posted_at(args.posted_at)
        except ValueError as exc:
            print(f"Invalid --posted-at value '{args.posted_at}': {exc}")
            print("Expected ISO 8601 format, e.g. '2026-03-23T09:42:00-04:00'")
            sys.exit(1)
    elif args.eastern:
        try:
            posted_at = _parse_eastern(args.eastern, now)
        except ValueError as exc:
            print(f"Invalid --eastern value '{args.eastern}': {exc}")
            sys.exit(1)
    else:
        posted_at = now

    # Show age and time-decay bucket whenever a real post time was supplied
    if args.posted_at or args.eastern:
        age_h = (now - posted_at).total_seconds() / 3600
        if age_h < 0:
            age_label = f"{abs(age_h):.1f}h in the future"
            decay_info = "N/A (future post)"
        else:
            age_label = f"{age_h:.1f}h ago"
            decay_info = _decay_label(age_h)
        print(f"Post time (UTC): {posted_at.strftime('%Y-%m-%d %H:%M')}  ({age_label})")
        print(f"Time-decay boost: {decay_info}")

    post_id = hashlib.sha1(content.encode()).hexdigest()[:16]

    data  = _load()
    posts = _prune_old(data.get("posts", []))

    # Check for duplicate
    existing_ids = {p.get("id", "") for p in posts}
    if post_id in existing_ids:
        if args.update_timestamp:
            # Find and update the timestamp (and URL if provided)
            updated = False
            for p in posts:
                if p.get("id") == post_id:
                    old_ts = p.get("posted_at", "")
                    p["posted_at"] = posted_at.isoformat()
                    if args.url:
                        p["url"] = args.url
                    updated = True
                    print(f"Updated post (id={post_id})")
                    print(f"  posted_at: {old_ts}  →  {posted_at.isoformat()}")
                    break
            if not updated:
                print(f"Error: could not find post id={post_id} to update.")
                sys.exit(1)
        else:
            print(f"Post already in file (id={post_id}) — skipping duplicate.")
            print("  Tip: use --update-timestamp to correct the stored post time.")
            data["fetched_at"] = now.isoformat()
            data["posts"] = posts
            _save(data)
            sys.exit(0)
    else:
        new_post = {
            "id":        post_id,
            "content":   content,
            "posted_at": posted_at.isoformat(),
            "url":       args.url,
        }
        posts.append(new_post)
        print(f"Added post (id={post_id}) — {len(posts)} total posts in file.")
        print(f"Content: \"{content[:80]}{'...' if len(content) > 80 else ''}\"")

    data["fetched_at"] = now.isoformat()
    data["posts"]      = posts
    _save(data)

    if args.process:
        _run_process_script()
    else:
        print("Signals will update within 15 minutes via MaintenanceRunner.")
        print("To apply immediately: python3 scripts/process_truth_social.py")
        print("                   or re-run with --process")


if __name__ == "__main__":
    main()
