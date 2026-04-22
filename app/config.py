from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    db_path: Path
    data_dir: Path
    raw_dir: Path
    action_cards_path: Path
    kalshi_mock: bool
    watcher_interval_sec: float
    transcript_interval_sec: float
    scorer_interval_sec: float
    transcript_urls: list[str]
    transcript_source: str
    transcript_http_timeout_sec: float
    # OpenClaw Browser Relay settings
    openclaw_scrape_cmd: str | None
    openclaw_timeout_sec: float
    openclaw_browser_profile: str
    # OpenClaw WhatsApp settings
    whatsapp_enabled: bool
    whatsapp_target: str
    # Scoring focus settings
    focus_event_markets: bool
    pre_event_window_sec: float
    event_seed_interval_sec: float
    # Liquidity gates
    max_spread: float
    min_depth: float
    ev_threshold: float
    # Market veto
    market_veto_margin: float
    poly_min_confidence: float
    wallet_flow_weight: float
    wallet_min_confidence: float
    adaptive_ev_scale: float
    source_agreement_weight: float
    # Trade guardrails
    pre_event_yes_threshold: float
    block_off_topic_yes: bool
    penny_price_threshold: float
    # Background maintenance (24/7 mode)
    maintenance_enabled: bool
    maintenance_interval_sec: float
    fetch_markets_interval_sec: float
    fetch_poly_interval_sec: float
    fetch_wallet_flow_interval_sec: float
    fetch_news_interval_sec: float
    fetch_x_interval_sec: float
    fetch_outcomes_interval_sec: float
    record_outcomes_interval_sec: float
    # Watchdog (stale data / scorer health)
    watchdog_enabled: bool
    watchdog_interval_sec: float
    watchdog_max_snapshot_age_sec: float
    watchdog_max_scorer_idle_sec: float
    watchdog_startup_grace_sec: float
    watchdog_breach_limit: int
    watchdog_exit_on_stale: bool
    # Scorer selection: use new Bayesian mispricing detector
    use_bayesian_scorer: bool


def _parse_urls(raw: str) -> list[str]:
    return [u.strip() for u in raw.split(",") if u.strip()]


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        val = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def load_settings() -> Settings:
    runtime_env_path = Path(os.getenv("RUNTIME_ENV_FILE", "config/runtime.env"))
    _load_env_file(runtime_env_path)

    data_dir = Path(os.getenv("DATA_DIR", "data"))
    raw_dir = data_dir / "raw"
    db_path = Path(os.getenv("DB_PATH", str(data_dir / "edge.db")))
    action_cards_path = Path(os.getenv("ACTION_CARDS_PATH", str(data_dir / "action_cards.jsonl")))

    kalshi_mock = os.getenv("KALSHI_MOCK", "1") == "1"
    focus_default = "0" if kalshi_mock else "1"
    maintenance_default = "0" if kalshi_mock else "1"
    watchdog_default = "0" if kalshi_mock else "1"

    return Settings(
        db_path=db_path,
        data_dir=data_dir,
        raw_dir=raw_dir,
        action_cards_path=action_cards_path,
        kalshi_mock=kalshi_mock,
        watcher_interval_sec=float(os.getenv("WATCHER_INTERVAL_SEC", "10")),
        transcript_interval_sec=float(os.getenv("TRANSCRIPT_INTERVAL_SEC", "30")),
        scorer_interval_sec=float(os.getenv("SCORER_INTERVAL_SEC", "10")),
        transcript_urls=_parse_urls(os.getenv("TRANSCRIPT_URLS", "")),
        transcript_source=os.getenv("TRANSCRIPT_SOURCE", "directhttp").strip().lower(),
        transcript_http_timeout_sec=float(os.getenv("TRANSCRIPT_HTTP_TIMEOUT_SEC", "15")),
        openclaw_scrape_cmd=os.getenv("OPENCLAW_SCRAPE_CMD"),
        openclaw_timeout_sec=float(os.getenv("OPENCLAW_TIMEOUT_SEC", "30")),
        openclaw_browser_profile=os.getenv("OPENCLAW_BROWSER_PROFILE", "openclaw"),
        whatsapp_enabled=os.getenv("WHATSAPP_ENABLED", "0") == "1",
        whatsapp_target=os.getenv("WHATSAPP_TARGET", ""),
        focus_event_markets=os.getenv("FOCUS_EVENT_MARKETS", focus_default) == "1",
        pre_event_window_sec=float(os.getenv("PRE_EVENT_WINDOW_SEC", "21600")),
        event_seed_interval_sec=float(os.getenv("EVENT_SEED_INTERVAL_SEC", "300")),
        max_spread=float(os.getenv("MAX_SPREAD", "0.15")),
        min_depth=float(os.getenv("MIN_DEPTH", "0")),
        ev_threshold=float(os.getenv("EV_THRESHOLD", "0.10")),
        market_veto_margin=float(os.getenv("MARKET_VETO_MARGIN", "0.10")),
        poly_min_confidence=float(os.getenv("POLY_MIN_CONFIDENCE", "0.35")),
        wallet_flow_weight=float(os.getenv("WALLET_FLOW_WEIGHT", "0.12")),
        wallet_min_confidence=float(os.getenv("WALLET_MIN_CONFIDENCE", "0.35")),
        adaptive_ev_scale=float(os.getenv("ADAPTIVE_EV_SCALE", "0.05")),
        source_agreement_weight=float(os.getenv("SOURCE_AGREEMENT_WEIGHT", "0.04")),
        pre_event_yes_threshold=float(os.getenv("PRE_EVENT_YES_THRESHOLD", "0.03")),
        block_off_topic_yes=os.getenv("BLOCK_OFF_TOPIC_YES", "0") == "1",
        penny_price_threshold=float(os.getenv("PENNY_PRICE_THRESHOLD", "0.00")),
        maintenance_enabled=os.getenv("MAINTENANCE_ENABLED", maintenance_default) == "1",
        maintenance_interval_sec=float(os.getenv("MAINTENANCE_INTERVAL_SEC", "60")),
        fetch_markets_interval_sec=float(os.getenv("FETCH_MARKETS_INTERVAL_SEC", "3600")),
        fetch_poly_interval_sec=float(os.getenv("FETCH_POLY_INTERVAL_SEC", "1800")),
        fetch_wallet_flow_interval_sec=float(os.getenv("FETCH_WALLET_FLOW_INTERVAL_SEC", "1800")),
        fetch_news_interval_sec=float(os.getenv("FETCH_NEWS_INTERVAL_SEC", "1800")),
        fetch_x_interval_sec=float(os.getenv("FETCH_X_INTERVAL_SEC", "1800")),
        fetch_outcomes_interval_sec=float(os.getenv("FETCH_OUTCOMES_INTERVAL_SEC", "3600")),
        record_outcomes_interval_sec=float(os.getenv("RECORD_OUTCOMES_INTERVAL_SEC", "900")),
        watchdog_enabled=os.getenv("WATCHDOG_ENABLED", watchdog_default) == "1",
        watchdog_interval_sec=float(os.getenv("WATCHDOG_INTERVAL_SEC", "60")),
        watchdog_max_snapshot_age_sec=float(os.getenv("WATCHDOG_MAX_SNAPSHOT_AGE_SEC", "300")),
        watchdog_max_scorer_idle_sec=float(os.getenv("WATCHDOG_MAX_SCORER_IDLE_SEC", "300")),
        watchdog_startup_grace_sec=float(os.getenv("WATCHDOG_STARTUP_GRACE_SEC", "180")),
        watchdog_breach_limit=int(os.getenv("WATCHDOG_BREACH_LIMIT", "3")),
        watchdog_exit_on_stale=os.getenv("WATCHDOG_EXIT_ON_STALE", "1") == "1",
        use_bayesian_scorer=os.getenv("USE_BAYESIAN_SCORER", "1") == "1",
    )
