#!/usr/bin/env python3
"""Compare MENTION_SERIES between fetch_markets and fetch_outcomes.

Run from repo root:
  python3 scripts/audit_mention_series.py

Exits 0 always; prints keys only in one file and speaker mismatches.
Manual-only project: informational audit only."""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _extract_dict(path: Path, name: str = "MENTION_SERIES") -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"^{name}\s*:\s*dict\[str,\s*str\]\s*=\s*\{{", text, re.MULTILINE)
    if not m:
        raise SystemExit(f"Could not find {name} in {path}")
    start = m.end() - 1
    depth = 0
    end = start
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    blob = text[start:end]
    return ast.literal_eval(blob)


def main() -> int:
    fm = ROOT / "scripts" / "fetch_markets.py"
    fo = ROOT / "scripts" / "fetch_outcomes.py"
    mkt = _extract_dict(fm)
    out = _extract_dict(fo)
    km, ko = set(mkt), set(out)
    only_m = sorted(km - ko)
    only_o = sorted(ko - km)
    if only_m:
        print("Only in fetch_markets.py (add to fetch_outcomes?):")
        for k in only_m:
            print(f"  {k}: {mkt[k]}")
    if only_o:
        print("Only in fetch_outcomes.py (remove or add to fetch_markets?):")
        for k in only_o:
            print(f"  {k}: {out[k]}")
    both = sorted(km & ko)
    mism = [(k, mkt[k], out[k]) for k in both if mkt[k] != out[k]]
    if mism:
        print("Speaker mismatch (same series, different label):")
        for k, a, b in mism:
            print(f"  {k}: markets={a!r} outcomes={b!r}")
    if not only_m and not only_o and not mism:
        print("MENTION_SERIES keys and speakers match between fetch_markets and fetch_outcomes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
