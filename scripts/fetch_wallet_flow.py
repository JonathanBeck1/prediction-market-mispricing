#!/usr/bin/env python3
"""Build wallet-flow alpha signals from public Polymarket trade activity.

V2 improvements:
  - Targeted per-market trade fetching (condition_id param)
  - Trade price parsing with conviction weighting
  - Extreme bet detection
  - Wallet reputation from /closed-positions
  - Time-decay weighting for recency
  - Maker/taker distinction
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
POLY_CACHE = Path("data/polymarket_prices.json")
OUTPUT_PATH = Path("data/wallet_signals.json")
REPUTATION_PATH = Path("data/wallet_reputation.json")

MAX_TRADES_PER_MARKET = 200
REPUTATION_TOP_N = 50
REPUTATION_CACHE_MAX_AGE_SEC = 3600 * 6


def _fetch_json(url: str, timeout: float = 20, max_attempts: int = 5) -> list | dict | None:
    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "kalshi-edge-wallet-flow/2.0",
        },
    )
    for attempt in range(max_attempts):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            if e.code == 429 and attempt < max_attempts - 1:
                time.sleep(min(8.0, 0.7 * (attempt + 1)))
                continue
            return None
        except (URLError, TimeoutError, OSError):
            if attempt < max_attempts - 1:
                time.sleep(min(5.0, 0.5 * (attempt + 1)))
                continue
            return None
    return None


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _extract_wallet(row: dict) -> str:
    for key in ("proxyWallet", "wallet", "user", "trader", "owner", "maker", "taker", "account"):
        val = row.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().lower()
        if isinstance(val, dict):
            for k2 in ("wallet", "address", "id"):
                v2 = val.get(k2)
                if isinstance(v2, str) and v2.strip():
                    return v2.strip().lower()
    return ""


def _extract_outcome(row: dict) -> str:
    text = " ".join(
        str(row.get(k, "")) for k in ("outcome", "side", "outcomeName", "position")
    ).lower()
    if "yes" in text:
        return "yes"
    if "no" in text:
        return "no"
    idx = str(row.get("outcomeIndex", "")).strip()
    if idx == "0":
        return "yes"
    if idx == "1":
        return "no"
    return ""


def _extract_action(row: dict) -> str:
    text = " ".join(
        str(row.get(k, "")) for k in ("action", "tradeSide", "direction", "type", "side")
    ).lower()
    if "buy" in text or "long" in text:
        return "buy"
    if "sell" in text or "short" in text:
        return "sell"
    return ""


def _extract_size(row: dict) -> float:
    for key in ("size", "amount", "shares", "quantity", "volume"):
        v = row.get(key)
        if v is not None:
            size = _to_float(v, default=0.0)
            if size > 0:
                return size
    return 0.0


def _extract_price(row: dict) -> float | None:
    for key in ("price", "avgPrice", "fillPrice", "executionPrice"):
        v = row.get(key)
        if v is not None:
            p = _to_float(v, default=-1.0)
            if 0.0 < p <= 1.0:
                return p
    return None


def _extract_timestamp(row: dict) -> float | None:
    for key in ("timestamp", "createdAt", "matchedAt", "time"):
        v = row.get(key)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            ts = float(v)
            if ts > 1e12:
                ts /= 1000.0
            return ts
        if isinstance(v, str):
            try:
                dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                return dt.timestamp()
            except Exception:
                pass
    return None


def _conviction_weight(price: float | None) -> float:
    """Weight based on how far from 0.5 the trade price is (stronger conviction = higher weight)."""
    if price is None:
        return 1.0
    return 1.0 + abs(price - 0.5) * 2.0


def _time_decay_weight(trade_ts: float | None, now_ts: float, half_life_sec: float = 7200.0) -> float:
    """Exponential decay: trades from half_life_sec ago get weight 0.5."""
    if trade_ts is None:
        return 0.5
    age = max(0.0, now_ts - trade_ts)
    return math.exp(-0.693 * age / max(half_life_sec, 60.0))


def _is_extreme_bet(price: float | None, action: str, outcome: str) -> bool:
    """Detect extreme conviction bets that most traders wouldn't take."""
    if price is None:
        return False
    if action == "buy":
        if outcome == "yes" and price <= 0.12:
            return True
        if outcome == "no" and price >= 0.88:
            return True
    if action == "sell":
        if outcome == "yes" and price >= 0.88:
            return True
        if outcome == "no" and price <= 0.12:
            return True
    return False


def _fetch_trades_for_market(condition_id: str, *, limit: int = MAX_TRADES_PER_MARKET) -> list[dict]:
    """Fetch trades for a specific market by condition_id."""
    rows: list[dict] = []
    seen: set[str] = set()
    offset = 0
    page_size = min(limit, 500)
    while len(rows) < limit:
        url = f"{DATA_API}/trades?market={condition_id}&limit={page_size}&offset={offset}"
        data = _fetch_json(url)
        if not isinstance(data, list) or not data:
            break
        new = 0
        for row in data:
            if not isinstance(row, dict):
                continue
            txh = str(row.get("transactionHash", "")).strip().lower()
            if txh and txh in seen:
                continue
            if txh:
                seen.add(txh)
            rows.append(row)
            new += 1
        if new == 0:
            break
        offset += page_size
        time.sleep(0.08)
    return rows[:limit]


def _fetch_trades_bulk_fallback(condition_ids: set[str], slugs: set[str], *, pages: int = 12) -> dict[str, list[dict]]:
    """Fallback: fetch recent trades globally and bucket by condition_id."""
    by_condition: dict[str, list[dict]] = {}
    seen: set[str] = set()
    for page in range(pages):
        url = f"{DATA_API}/trades?limit=500&offset={page * 500}"
        data = _fetch_json(url)
        if not isinstance(data, list) or not data:
            break
        new = 0
        for row in data:
            if not isinstance(row, dict):
                continue
            txh = str(row.get("transactionHash", "")).strip().lower()
            if txh and txh in seen:
                continue
            if txh:
                seen.add(txh)
            cid = str(row.get("conditionId", "")).strip().lower()
            slug = str(row.get("slug", "")).strip().lower()
            if cid and cid in condition_ids:
                by_condition.setdefault(cid, []).append(row)
                new += 1
            elif slug and slug in slugs:
                by_condition.setdefault(slug, []).append(row)
                new += 1
        if new == 0 and page > 2:
            break
        time.sleep(0.1)
    return by_condition


def _compute_signal(trades: list[dict], now_ts: float) -> dict:
    """Compute conviction-weighted wallet flow signal from trades."""
    wallet_stats: dict[str, dict] = {}
    net_yes_flow = 0.0
    conviction_weighted_flow = 0.0
    total_volume = 0.0
    trade_count = 0
    extreme_bets: list[dict] = []
    price_sum = 0.0
    price_count = 0

    for t in trades:
        wallet = _extract_wallet(t)
        outcome = _extract_outcome(t)
        action = _extract_action(t)
        size = _extract_size(t)
        price = _extract_price(t)
        trade_ts = _extract_timestamp(t)

        if not wallet or outcome not in {"yes", "no"} or action not in {"buy", "sell"} or size <= 0:
            continue

        conv_w = _conviction_weight(price)
        time_w = _time_decay_weight(trade_ts, now_ts)
        combined_w = conv_w * time_w

        signed = size if action == "buy" else -size
        flow = signed if outcome == "yes" else -signed
        weighted_flow = flow * combined_w

        net_yes_flow += flow
        conviction_weighted_flow += weighted_flow
        total_volume += size
        trade_count += 1

        if price is not None:
            price_sum += price
            price_count += 1

        if _is_extreme_bet(price, action, outcome):
            extreme_bets.append({
                "wallet": wallet[:12] + "...",
                "action": action,
                "outcome": outcome,
                "price": round(price, 4) if price else None,
                "size": round(size, 2),
            })

        st = wallet_stats.setdefault(wallet, {
            "net_yes_flow": 0.0, "weighted_flow": 0.0,
            "volume": 0.0, "trades": 0, "extreme_count": 0,
        })
        st["net_yes_flow"] += flow
        st["weighted_flow"] += weighted_flow
        st["volume"] += size
        st["trades"] += 1
        if _is_extreme_bet(price, action, outcome):
            st["extreme_count"] += 1

    unique_wallets = len(wallet_stats)
    weighted_ratio = conviction_weighted_flow / max(1.0, total_volume)
    wallet_alpha_score = max(-1.0, min(1.0, math.tanh(weighted_ratio * 1.8)))

    confidence = min(
        1.0,
        min(1.0, total_volume / 200.0)
        * min(1.0, trade_count / 30.0)
        * min(1.0, unique_wallets / 8.0),
    )

    bias = "neutral"
    if wallet_alpha_score >= 0.08:
        bias = "up"
    elif wallet_alpha_score <= -0.08:
        bias = "down"

    top_wallets = sorted(
        (
            {
                "wallet": w[:12] + "...",
                "wallet_full": w,
                "net_yes_flow": round(v["net_yes_flow"], 4),
                "weighted_flow": round(v["weighted_flow"], 4),
                "volume": round(v["volume"], 4),
                "trades": v["trades"],
                "extreme_count": v["extreme_count"],
            }
            for w, v in wallet_stats.items()
        ),
        key=lambda x: abs(x["weighted_flow"]),
        reverse=True,
    )[:10]

    return {
        "smart_flow_bias": bias,
        "wallet_alpha_score": round(wallet_alpha_score, 4),
        "confidence": round(confidence, 4),
        "net_yes_flow": round(net_yes_flow, 4),
        "conviction_weighted_flow": round(conviction_weighted_flow, 4),
        "total_volume": round(total_volume, 4),
        "trade_count": trade_count,
        "unique_wallets": unique_wallets,
        "avg_trade_price": round(price_sum / max(1, price_count), 4),
        "extreme_bet_count": len(extreme_bets),
        "extreme_bets": extreme_bets[:5],
        "top_wallets": top_wallets,
    }


def _load_wallet_reputation() -> dict[str, dict]:
    """Load cached wallet reputation scores."""
    if not REPUTATION_PATH.exists():
        return {}
    try:
        data = json.loads(REPUTATION_PATH.read_text(encoding="utf-8"))
        fetched = data.get("fetched_at", "")
        if fetched:
            dt = datetime.fromisoformat(fetched.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - dt).total_seconds()
            if age > REPUTATION_CACHE_MAX_AGE_SEC:
                return {}
        return {w["wallet"]: w for w in data.get("wallets", []) if isinstance(w, dict)}
    except Exception:
        return {}


def _build_wallet_reputation(top_wallet_addresses: list[str]) -> dict[str, dict]:
    """Query /activity and /closed-positions for top wallets to estimate profitability."""
    reputation: dict[str, dict] = {}
    unique = list(dict.fromkeys(top_wallet_addresses))[:REPUTATION_TOP_N]

    for addr in unique:
        if not addr or len(addr) < 10:
            continue

        closed = _fetch_json(f"{DATA_API}/closed-positions?user={addr}&limit=100")
        if not isinstance(closed, list):
            closed = []

        wins = 0
        losses = 0
        total_pnl = 0.0
        for pos in closed:
            if not isinstance(pos, dict):
                continue
            pnl = _to_float(pos.get("pnl") or pos.get("realizedPnl") or pos.get("profit"), 0.0)
            total_pnl += pnl
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1

        total_resolved = wins + losses
        win_rate = wins / max(1, total_resolved)
        rep_score = min(1.0, (win_rate * 0.6 + min(1.0, total_resolved / 50.0) * 0.4))

        reputation[addr] = {
            "wallet": addr,
            "wins": wins,
            "losses": losses,
            "total_resolved": total_resolved,
            "win_rate": round(win_rate, 4),
            "total_pnl": round(total_pnl, 2),
            "reputation_score": round(rep_score, 4),
        }
        time.sleep(0.15)

    return reputation


def _save_reputation(reputation: dict[str, dict]) -> None:
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "wallet_count": len(reputation),
        "wallets": list(reputation.values()),
    }
    REPUTATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPUTATION_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    print(f"Saved wallet reputation for {len(reputation)} wallets to {REPUTATION_PATH}")


def _iter_poly_targets() -> list[dict]:
    if not POLY_CACHE.exists():
        return []
    data = json.loads(POLY_CACHE.read_text(encoding="utf-8"))
    out: list[dict] = []
    for event in data.get("events", []):
        speaker = str(event.get("speaker", "")).strip().lower()
        event_title = str(event.get("title", "")).strip()
        timeframe = str(event.get("timeframe", "unknown")).strip().lower()
        for m in event.get("markets", []):
            poly_id = str(m.get("poly_id", "")).strip()
            phrase = str(m.get("phrase_norm") or m.get("phrase") or "").strip().lower()
            if not poly_id or not phrase or not speaker:
                continue
            out.append({
                "poly_id": poly_id,
                "phrase": phrase,
                "speaker": speaker,
                "event_title": event_title,
                "timeframe": timeframe,
                "kalshi_ticker": str(m.get("kalshi_ticker", "")).strip(),
                "condition_id": str(m.get("condition_id", "")).strip().lower(),
                "poly_slug": str(m.get("slug", "")).strip().lower(),
            })
    return out


def main() -> None:
    targets = _iter_poly_targets()
    if not targets:
        print("No usable Polymarket targets found. Run fetch_polymarket first.")
        return

    now_ts = datetime.now(timezone.utc).timestamp()

    condition_ids_to_fetch = [t["condition_id"] for t in targets if t.get("condition_id")]
    condition_id_set = set(condition_ids_to_fetch)
    slug_set = {t["poly_slug"] for t in targets if t.get("poly_slug")}

    print(f"Fetching targeted trades for {len(condition_id_set)} markets...")

    trades_by_cid: dict[str, list[dict]] = {}
    fetched_targeted = 0
    targeted_limit = 30

    priority_cids = list(condition_id_set)[:targeted_limit]
    for i, cid in enumerate(priority_cids, 1):
        if not cid:
            continue
        trades = _fetch_trades_for_market(cid, limit=MAX_TRADES_PER_MARKET)
        if trades:
            trades_by_cid[cid] = trades
            fetched_targeted += 1
        if i % 10 == 0:
            print(f"  fetched {i}/{len(priority_cids)} markets ({fetched_targeted} with trades)...")
        time.sleep(0.1)

    remaining_cids = condition_id_set - set(trades_by_cid.keys())
    if remaining_cids:
        print(f"Bulk-fetching trades for {len(remaining_cids)} remaining markets...")
        bulk = _fetch_trades_bulk_fallback(remaining_cids, slug_set)
        for cid, trades in bulk.items():
            if cid not in trades_by_cid:
                trades_by_cid[cid] = trades

    print(
        f"Fetched trades for {len(trades_by_cid)} markets "
        f"({fetched_targeted} targeted, {len(trades_by_cid) - fetched_targeted} from bulk)."
    )

    all_top_wallets: list[str] = []

    signals = []
    print(f"Building wallet-flow signals for {len(targets)} markets...")
    for i, t in enumerate(targets, start=1):
        cid = t["condition_id"]
        trades = trades_by_cid.get(cid, [])
        match_method = "targeted" if cid in trades_by_cid else "none"
        if not trades and t["poly_slug"] in trades_by_cid:
            trades = trades_by_cid[t["poly_slug"]]
            match_method = "slug_bulk"

        signal = _compute_signal(trades, now_ts)

        for tw in signal.get("top_wallets", []):
            wf = tw.get("wallet_full", "")
            if wf:
                all_top_wallets.append(wf)

        signals.append({
            "poly_id": t["poly_id"],
            "phrase": t["phrase"],
            "speaker": t["speaker"],
            "event_title": t["event_title"],
            "timeframe": t["timeframe"],
            "kalshi_ticker": t["kalshi_ticker"],
            "condition_id": t["condition_id"],
            "poly_slug": t["poly_slug"],
            "match_method": match_method,
            **signal,
        })
        if i % 25 == 0:
            print(f"  processed {i}/{len(targets)}...")

    existing_reputation = _load_wallet_reputation()
    if all_top_wallets:
        unique_top = list(dict.fromkeys(all_top_wallets))
        new_wallets = [w for w in unique_top if w not in existing_reputation]
        if new_wallets or not existing_reputation:
            wallets_to_check = (new_wallets + list(existing_reputation.keys()))[:REPUTATION_TOP_N]
            print(f"Building reputation for {len(wallets_to_check)} wallets...")
            reputation = _build_wallet_reputation(wallets_to_check)
            if reputation:
                existing_reputation.update(reputation)
                _save_reputation(existing_reputation)
        else:
            print(f"Using cached reputation for {len(existing_reputation)} wallets.")

    for sig in signals:
        top_ws = sig.get("top_wallets", [])
        rep_boost = 0.0
        rep_count = 0
        for tw in top_ws:
            wf = tw.get("wallet_full", "")
            rep = existing_reputation.get(wf)
            if rep and rep.get("reputation_score", 0) > 0:
                tw["reputation_score"] = rep["reputation_score"]
                tw["win_rate"] = rep.get("win_rate", 0)
                tw["total_resolved"] = rep.get("total_resolved", 0)
                rep_boost += rep["reputation_score"] * abs(tw.get("weighted_flow", 0))
                rep_count += 1
            tw.pop("wallet_full", None)

        if rep_count > 0 and sig["total_volume"] > 0:
            normalized_rep = rep_boost / max(1.0, sig["total_volume"])
            sig["reputation_weighted_alpha"] = round(
                max(-1.0, min(1.0, math.tanh(normalized_rep * 2.0))), 4
            )
        else:
            sig["reputation_weighted_alpha"] = 0.0

    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "version": 2,
        "total_markets": len(targets),
        "signal_count": len(signals),
        "markets_with_trades": sum(1 for s in signals if s["trade_count"] > 0),
        "markets_with_extreme_bets": sum(1 for s in signals if s["extreme_bet_count"] > 0),
        "reputation_wallets_used": len(existing_reputation),
        "signals": signals,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    print(f"\nSaved wallet signals: {OUTPUT_PATH} ({len(signals)} rows)")
    print(f"  Markets with trades: {payload['markets_with_trades']}")
    print(f"  Markets with extreme bets: {payload['markets_with_extreme_bets']}")
    print(f"  Reputation wallets: {payload['reputation_wallets_used']}")


if __name__ == "__main__":
    main()
