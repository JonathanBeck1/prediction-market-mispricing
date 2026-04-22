#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required for X pipeline: pip install pyyaml") from exc

API_BASE = "https://api.x.com/2"

WATCHLIST_PATH = Path("config/x_watchlist.yaml")
STATE_PATH = Path("data/x_state.json")
USAGE_PATH = Path("data/x_usage.json")
POSTS_PATH = Path("data/x_posts.jsonl")
MARKETS_PATH = Path("data/kalshi_markets.json")
SIGNALS_PATH = Path("data/signals.yaml")

POST_READ_COST = 0.005
USER_READ_COST = 0.010


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _save_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _load_signals_map(path: Path) -> dict[str, dict]:
    payload = _load_yaml(path)
    rows = payload.get("signals", []) if isinstance(payload, dict) else []
    out: dict[str, dict] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        phrase = str(row.get("phrase", "")).strip().lower()
        if not phrase:
            continue
        # Preserve ALL fields (including llm_boost, llm_reasoning, etc.) so
        # LLM-written values survive subsequent signal-fetch runs.
        node = dict(row)
        node["phrase"] = phrase
        node.setdefault("news_pressure", 1.0)
        node.setdefault("x_buzz", 1.0)
        out[phrase] = node
    return out


def _save_signals_map(path: Path, signal_map: dict[str, dict]) -> None:
    rows = []
    for phrase in sorted(signal_map.keys()):
        node = dict(signal_map[phrase])
        node["phrase"] = phrase
        node["news_pressure"] = float(node.get("news_pressure", 1.0))
        node["x_buzz"] = float(node.get("x_buzz", 1.0))
        node["updated_at"] = str(node.get("updated_at", "")).strip() or _now_iso()
        rows.append(node)
    _save_yaml(path, {"signals": rows})


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _fetch_json(url: str, bearer: str) -> dict:
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {bearer}",
            "User-Agent": "kalshi-edge-x-pipeline/1.0",
        },
    )
    with urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _resolve_user_id(username: str, bearer: str) -> str | None:
    url = f"{API_BASE}/users/by/username/{username}?user.fields=id,username,name"
    try:
        payload = _fetch_json(url, bearer)
    except (HTTPError, URLError, TimeoutError, OSError):
        return None
    data = payload.get("data", {})
    uid = str(data.get("id", "")).strip()
    return uid or None


def _fetch_user_posts(user_id: str, bearer: str, since_id: str | None, max_results: int) -> list[dict]:
    params = {
        "max_results": str(max_results),
        "tweet.fields": "created_at,public_metrics",
        "exclude": "retweets,replies",
    }
    if since_id:
        params["since_id"] = since_id
    url = f"{API_BASE}/users/{user_id}/tweets?{urlencode(params)}"
    try:
        payload = _fetch_json(url, bearer)
    except (HTTPError, URLError, TimeoutError, OSError):
        return []
    rows = payload.get("data", [])
    return rows if isinstance(rows, list) else []


def _load_phrase_universe() -> list[str]:
    data = _load_json(MARKETS_PATH, {})
    phrases: set[str] = set()
    for m in data.get("markets", []):
        primary = str(m.get("primary_phrase", "")).strip().lower()
        if primary:
            phrases.add(primary)
        for pv in m.get("phrase_variants", []):
            p = str(pv).strip().lower()
            if p:
                phrases.add(p)
    return sorted(phrases)


def _match_scores(text: str, phrases: list[str], weight: float) -> dict[str, float]:
    out: dict[str, float] = {}
    text_lower = text.lower()
    for phrase in phrases:
        if len(phrase) < 3:
            continue
        pattern = r"\b" + re.escape(phrase) + r"\b"
        if re.search(pattern, text_lower):
            out[phrase] = out.get(phrase, 0.0) + weight
    return out


def _update_signals(scores: dict[str, float]) -> int:
    signals = _load_signals_map(SIGNALS_PATH)
    updated = 0
    for phrase, score in scores.items():
        node = signals.get(phrase)
        if not isinstance(node, dict):
            node = {"phrase": phrase, "news_pressure": 1.0, "x_buzz": 1.0, "updated_at": None}
            signals[phrase] = node
        target = 1.0 + min(score, 25.0) * 0.02
        target = max(0.7, min(1.5, round(target, 2)))
        if float(node.get("x_buzz", 1.0)) != target:
            node["x_buzz"] = target
            node["updated_at"] = _now_iso()
            updated += 1
    _save_signals_map(SIGNALS_PATH, signals)
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch X posts for tracked accounts and derive deterministic x_buzz")
    parser.add_argument("--max-results", type=int, default=10, help="Max posts per account poll (default: 10)")
    args = parser.parse_args()

    bearer = os.getenv("X_BEARER_TOKEN", "").strip()
    if not bearer:
        raise SystemExit("Missing X_BEARER_TOKEN")

    monthly_budget = float(os.getenv("X_MONTHLY_BUDGET_USD", "25"))
    daily_post_cap = int(os.getenv("X_DAILY_POST_RESOURCE_CAP", "300"))

    watch = _load_yaml(WATCHLIST_PATH)
    accounts = watch.get("accounts", [])
    if not isinstance(accounts, list) or not accounts:
        raise SystemExit(f"No accounts configured in {WATCHLIST_PATH}")

    state = _load_json(STATE_PATH, {"accounts": {}})
    usage = _load_json(USAGE_PATH, {"month": "", "posts_read": 0, "users_read": 0, "daily": {}})

    now = datetime.now(timezone.utc)
    month_key = now.strftime("%Y-%m")
    day_key = now.strftime("%Y-%m-%d")

    if usage.get("month") != month_key:
        usage = {"month": month_key, "posts_read": 0, "users_read": 0, "daily": {}}
    usage.setdefault("daily", {})
    day_posts = int(usage["daily"].get(day_key, 0))

    month_spend = float(usage.get("posts_read", 0)) * POST_READ_COST + float(usage.get("users_read", 0)) * USER_READ_COST
    if month_spend >= monthly_budget:
        raise SystemExit(f"Monthly X budget exceeded (${month_spend:.2f} >= ${monthly_budget:.2f})")
    if day_posts >= daily_post_cap:
        raise SystemExit(f"Daily X post-resource cap reached ({day_posts} >= {daily_post_cap})")

    phrases = _load_phrase_universe()
    score_map: dict[str, float] = {}
    fetched_posts = 0
    fetched_users = 0

    POSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with POSTS_PATH.open("a", encoding="utf-8") as fh:
        for acct in accounts:
            username = str(acct.get("username", "")).strip().lstrip("@")
            if not username:
                continue
            priority = str(acct.get("priority", "medium")).strip().lower()
            weight = 2.0 if priority == "high" else 0.5 if priority == "low" else 1.0

            node = state.setdefault("accounts", {}).setdefault(username, {})
            user_id = str(node.get("user_id", "")).strip()
            if not user_id:
                user_id = _resolve_user_id(username, bearer) or ""
                if not user_id:
                    continue
                node["user_id"] = user_id
                fetched_users += 1

            since_id = str(node.get("since_id", "")).strip() or None
            posts = _fetch_user_posts(user_id, bearer, since_id, args.max_results)
            if not posts:
                continue

            max_id = since_id or ""
            for post in posts:
                pid = str(post.get("id", "")).strip()
                text = str(post.get("text", "")).strip()
                if not pid or not text:
                    continue
                max_id = max(max_id, pid)
                rec = {
                    "ts": _now_iso(),
                    "source": "x",
                    "username": username,
                    "user_id": user_id,
                    "post_id": pid,
                    "text": text,
                    "created_at": post.get("created_at"),
                    "priority": priority,
                    "weight": weight,
                }
                fh.write(json.dumps(rec, ensure_ascii=True) + "\n")
                fetched_posts += 1
                for phrase, score in _match_scores(text, phrases, weight).items():
                    score_map[phrase] = score_map.get(phrase, 0.0) + score

            node["since_id"] = max_id
            node["last_polled_at"] = _now_iso()

            day_posts += len(posts)
            if day_posts >= daily_post_cap:
                break

    usage["posts_read"] = int(usage.get("posts_read", 0)) + fetched_posts
    usage["users_read"] = int(usage.get("users_read", 0)) + fetched_users
    usage["daily"][day_key] = day_posts
    _save_json(STATE_PATH, state)
    _save_json(USAGE_PATH, usage)

    changed = _update_signals(score_map)
    spend_now = float(usage["posts_read"]) * POST_READ_COST + float(usage["users_read"]) * USER_READ_COST

    print(f"Accounts configured: {len(accounts)}")
    print(f"Posts fetched this run: {fetched_posts}")
    print(f"User lookups this run: {fetched_users}")
    print(f"Phrases updated in signals.yaml: {changed}")
    print(f"Month-to-date spend estimate: ${spend_now:.2f} (budget ${monthly_budget:.2f})")


if __name__ == "__main__":
    main()
