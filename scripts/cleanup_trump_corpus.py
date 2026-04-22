#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import textwrap
from pathlib import Path


CANONICAL_TYPES = {
    "address",
    "presser",
    "interview",
    "rally",
    "briefing",
    "townhall",
    "announcement",
    "general",
}

SPEAKER_LINE = re.compile(r"^([A-Za-z][A-Za-z0-9 .'\-]{1,80}):\s*(.*)$")
KNOWN_SPEAKER_TOKEN = (
    r"(?:Donald Trump|The President|President [A-Za-z][A-Za-z.\-']+|Speaker \d+|Mike Pence|"
    r"Dr Fauci|Alex Azar|Robert Wilkie|Seema Verma|Bill Lee|Joseph Lengyel|Bob)"
)


def _normalize_text(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u00b7", " ").replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u00a0", " ")

    # Convert patterns like:
    # "Donald Trump: (\n00:00\n) text..." -> "Donald Trump: text..."
    text = re.sub(r":\s*\(\s*\n\s*\d{1,2}:\d{2}\s*\n\s*\)\s*", ": ", text)
    text = re.sub(r":\s*\(\s*\d{1,2}:\d{2}\s*\)\s*", ": ", text)

    # Merge split speaker lines:
    # "President Zelensky\n: text" -> "President Zelensky: text"
    text = re.sub(r"\n([A-Za-z][^\n:]{1,80})\n:\s*", r"\n\1: ", text)
    # Split embedded speaker labels back onto their own lines:
    # "... sentence. Donald Trump: ..." -> "... sentence.\nDonald Trump: ..."
    text = re.sub(r"([.!?])\s+([A-Z][A-Za-z0-9 .'\-]{1,80}):\s+", r"\1\n\2: ", text)
    text = re.sub(rf"\s+({KNOWN_SPEAKER_TOKEN}):\s+", r"\n\1: ", text)

    # Trim line tails and collapse huge blank streaks.
    lines = [ln.rstrip() for ln in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def _wrap_speaker_block(text: str, width: int = 120) -> str:
    out: list[str] = []
    current_speaker: str | None = None
    current_buf: list[str] = []

    def flush() -> None:
        nonlocal current_speaker, current_buf
        if not current_buf:
            return
        joined = " ".join(p.strip() for p in current_buf if p.strip())
        joined = re.sub(r"\s{2,}", " ", joined).strip()
        if not joined:
            current_speaker = None
            current_buf = []
            return

        wrapped = textwrap.fill(
            joined,
            width=width,
            break_long_words=False,
            break_on_hyphens=False,
        )
        if current_speaker:
            out.append(f"{current_speaker}: {wrapped}")
        else:
            out.append(wrapped)
        current_speaker = None
        current_buf = []

    for raw_ln in text.splitlines():
        ln = raw_ln.strip()
        if not ln:
            flush()
            if out and out[-1] != "":
                out.append("")
            continue

        m = SPEAKER_LINE.match(ln)
        if m:
            flush()
            current_speaker = m.group(1).strip()
            rest = m.group(2).strip()
            if rest:
                current_buf.append(rest)
        else:
            current_buf.append(ln)

    flush()

    # Final newline.
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out) + "\n"


def _guess_type(text: str) -> str:
    t = text.lower()

    if "town hall" in t or "townhall" in t:
        return "townhall"
    if "interview" in t or "on fox" in t or "podcast" in t:
        return "interview"
    if "press briefing" in t or ("reporter:" in t and "press" in t):
        return "briefing"
    if "press conference" in t or "q&a" in t or ("reporter:" in t and "white house" in t):
        return "presser"
    if "rally" in t or "crowd" in t and "applause" in t:
        return "rally"
    if "executive order" in t or "proclamation" in t or "signing" in t:
        return "announcement"
    if "remarks" in t or "address" in t or "thank you very much" in t:
        return "address"
    return "address"


def _parse_name(path: Path) -> tuple[str, str, str]:
    parts = path.stem.split("_")
    event_type = parts[0] if len(parts) >= 1 else "general"
    date = parts[1] if len(parts) >= 2 else "1970-01-01"
    seq = parts[2] if len(parts) >= 3 else "01"
    if event_type not in CANONICAL_TYPES:
        event_type = "general"
    return event_type, date, seq


def _next_available_name(dir_path: Path, event_type: str, date: str, seq_hint: str) -> Path:
    candidate = dir_path / f"{event_type}_{date}_{seq_hint}.txt"
    if not candidate.exists():
        return candidate
    i = 1
    while True:
        seq = f"{i:02d}"
        candidate = dir_path / f"{event_type}_{date}_{seq}.txt"
        if not candidate.exists():
            return candidate
        i += 1


def cleanup_trump_corpus(target_dir: Path, dry_run: bool = False) -> None:
    files = sorted(target_dir.glob("*.txt"))
    rewritten = 0
    renamed = 0

    for path in files:
        raw = path.read_text(encoding="utf-8", errors="replace")
        normalized = _normalize_text(raw)
        wrapped = _wrap_speaker_block(normalized)

        if wrapped != raw:
            rewritten += 1
            if not dry_run:
                path.write_text(wrapped, encoding="utf-8")

        event_type, date, seq = _parse_name(path)
        if event_type == "general":
            new_type = _guess_type(wrapped)
            if new_type != "general":
                target = _next_available_name(target_dir, new_type, date, seq)
                if target != path:
                    renamed += 1
                    if not dry_run:
                        path.rename(target)

    remaining_general = len(list(target_dir.glob("general_*.txt")))
    total_files = len(list(target_dir.glob("*.txt")))
    print(f"total_files={total_files}")
    print(f"rewritten_files={rewritten}")
    print(f"renamed_from_general={renamed}")
    print(f"remaining_general_files={remaining_general}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize and relabel Trump corpus transcripts.")
    parser.add_argument(
        "--target-dir",
        default="data/corpus/trump",
        help="Directory containing Trump corpus files (default: data/corpus/trump)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Analyze only, no writes/renames")
    args = parser.parse_args()

    target = Path(args.target_dir)
    if not target.exists():
        raise SystemExit(f"Missing target dir: {target}")
    cleanup_trump_corpus(target, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
