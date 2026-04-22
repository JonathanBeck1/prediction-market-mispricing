SHELL := /bin/zsh

.PHONY: doctor run run-live run-live-24x7 refresh-live refresh-live-deep event-ready health-check test scrape analyze calibrate fetch-markets fetch-outcomes fetch-poly fetch-wallet fetch-news fetch-x dashboard ingest-corpus prune record-outcomes report-outcomes backtest tune-policy compute-cooccurrence fetch-signals analyze-signals signal-refresh install-24x7 install-24x7-all uninstall-24x7 uninstall-24x7-all restart-24x7 status-24x7 local-up local-up-fresh local-down local-restart local-status

doctor:
	@echo "pwd: $$(pwd)"
	@echo "which python3: $$(which python3 || echo 'python3 not found')"
	@if [ -d app ] && [ -f main.py ]; then \
		echo "repo root check: OK (app/ and main.py present)"; \
	else \
		echo "repo root check: FAIL (run from directory containing app/ and main.py)"; \
		exit 1; \
	fi
	@if [ -f app/__init__.py ]; then \
		echo "package check: OK (app/__init__.py present)"; \
	else \
		echo "package check: FAIL (missing app/__init__.py)"; \
		exit 1; \
	fi
	@if [ -d .venv ]; then \
		echo "venv check: OK (.venv exists)"; \
	else \
		echo "venv check: MISSING (.venv)"; \
		echo "create with: python3 -m venv .venv"; \
	fi

run:
	@if [ ! -d app ] || [ ! -f main.py ]; then \
		echo "Run from repo root (must contain app/ and main.py)."; \
		exit 1; \
	fi
	@if [ -f .venv/bin/activate ]; then \
		source .venv/bin/activate; \
		echo "using venv: $$VIRTUAL_ENV"; \
		KALSHI_MOCK=$${KALSHI_MOCK:-1} python3 -m app.runner; \
	else \
		echo ".venv not found."; \
		echo "create and install deps:"; \
		echo "  python3 -m venv .venv"; \
		echo "  source .venv/bin/activate"; \
		echo "  python3 -m pip install -r requirements.txt"; \
		exit 1; \
	fi

run-live:
	@if [ ! -d app ] || [ ! -f main.py ]; then \
		echo "Run from repo root (must contain app/ and main.py)."; \
		exit 1; \
	fi
	@if [ -f .venv/bin/activate ]; then \
		source .venv/bin/activate; \
		echo "using venv: $$VIRTUAL_ENV"; \
		KALSHI_MOCK=0 PRE_EVENT_WINDOW_SEC=604800 FETCH_MARKETS_INTERVAL_SEC=600 python3 -m app.runner; \
	else \
		echo ".venv not found."; \
		echo "create and install deps:"; \
		echo "  python3 -m venv .venv"; \
		echo "  source .venv/bin/activate"; \
		echo "  python3 -m pip install -r requirements.txt"; \
		exit 1; \
	fi

run-live-24x7:
	@if [ ! -d app ] || [ ! -f main.py ]; then \
		echo "Run from repo root (must contain app/ and main.py)."; \
		exit 1; \
	fi
	@if [ -f .venv/bin/activate ]; then \
		source .venv/bin/activate; \
		echo "using venv: $$VIRTUAL_ENV"; \
		KALSHI_MOCK=0 MAINTENANCE_ENABLED=1 PRE_EVENT_WINDOW_SEC=604800 FETCH_MARKETS_INTERVAL_SEC=600 python3 -m app.runner; \
	else \
		echo ".venv not found."; \
		echo "create and install deps:"; \
		echo "  python3 -m venv .venv"; \
		echo "  source .venv/bin/activate"; \
		echo "  python3 -m pip install -r requirements.txt"; \
		exit 1; \
	fi

refresh-live:
	@echo "Refreshing live market + Poly + wallet + news + X caches ..."
	@source .venv/bin/activate && \
		python3 scripts/fetch_markets.py && \
		python3 scripts/fetch_polymarket.py && \
		python3 scripts/fetch_wallet_flow.py && \
		python3 scripts/fetch_news_signals.py && \
		if [ -n "$$X_BEARER_TOKEN" ]; then \
			python3 scripts/fetch_x_signals.py; \
		else \
			echo "Skipping X refresh (X_BEARER_TOKEN not set)."; \
		fi

refresh-live-deep:
	@echo "Refreshing live caches including outcomes + wallet + news ..."
	@source .venv/bin/activate && \
		python3 scripts/fetch_markets.py && \
		python3 scripts/fetch_polymarket.py && \
		python3 scripts/fetch_wallet_flow.py && \
		python3 scripts/fetch_news_signals.py && \
		if [ -n "$$X_BEARER_TOKEN" ]; then \
			python3 scripts/fetch_x_signals.py; \
		else \
			echo "Skipping X refresh (X_BEARER_TOKEN not set)."; \
		fi && \
		python3 scripts/fetch_outcomes.py

event-ready:
	@echo "Running canonical pre-event refresh + health checks ..."
	@source .venv/bin/activate && \
		$(MAKE) refresh-live && \
		python3 scripts/health_check.py --max-snapshot-age-sec 31536000 --max-card-age-sec 31536000 --min-wallet-present-ratio 0.0

health-check:
	@echo "Running runtime health checks ..."
	@source .venv/bin/activate && python3 scripts/health_check.py

test:
	@if [ ! -d app ] || [ ! -f main.py ]; then \
		echo "Run from repo root (must contain app/ and main.py)."; \
		exit 1; \
	fi
	@if [ -f .venv/bin/activate ]; then \
		source .venv/bin/activate; \
		python3 -m pytest -q; \
		rc=$$?; \
		if [ $$rc -eq 5 ]; then \
			echo "No tests collected (expected until tests are added)."; \
			exit 0; \
		fi; \
		exit $$rc; \
	else \
		echo ".venv not found."; \
		echo "create and install deps first."; \
		exit 1; \
	fi

fetch-markets:
	@echo "Fetching real mention markets from Kalshi API ..."
	@source .venv/bin/activate && python3 scripts/fetch_markets.py

fetch-outcomes:
	@echo "Fetching finalized mention-market outcomes from Kalshi API ..."
	@source .venv/bin/activate && python3 scripts/fetch_outcomes.py

scrape:
	@echo "Scraping transcripts into data/corpus/ ..."
	@source .venv/bin/activate && python3 scripts/scrape_corpus.py

analyze:
	@echo "Analyzing corpus for phrase hit rates ..."
	@source .venv/bin/activate && python3 scripts/analyze_corpus.py

calibrate:
	@echo "Calibrating base rates from corpus ..."
	@source .venv/bin/activate && python3 scripts/calibrate_base_rates.py

fetch-poly:
	@echo "Fetching Polymarket mention prices + cross-matching ..."
	@source .venv/bin/activate && python3 scripts/fetch_polymarket.py

fetch-wallet:
	@echo "Fetching wallet-flow alpha signals from Polymarket activity ..."
	@source .venv/bin/activate && python3 scripts/fetch_wallet_flow.py

fetch-news:
	@echo "Fetching free news feed signals ..."
	@source .venv/bin/activate && python3 scripts/fetch_news_signals.py

fetch-x:
	@echo "Fetching X posts for deterministic x_buzz signals ..."
	@source .venv/bin/activate && python3 scripts/fetch_x_signals.py

ingest-corpus:
	@echo "Ingesting corpus transcripts into SQLite (phrase matching) ..."
	@source .venv/bin/activate && python3 scripts/ingest_corpus.py --fresh

dashboard:
	@if [ -f .venv/bin/activate ]; then \
		source .venv/bin/activate; \
		python3 -m app.dashboard; \
	else \
		echo ".venv not found."; \
		exit 1; \
	fi

prune:
	@echo "Pruning stale market snapshots (keeping last 3 days) ..."
	@source .venv/bin/activate && python3 scripts/prune_snapshots.py

record-outcomes:
	@echo "Recording settled outcomes vs decision-time BUY cards ..."
	@source .venv/bin/activate && python3 scripts/record_outcomes.py

report-outcomes:
	@echo "Reporting realized outcomes and P&L ..."
	@source .venv/bin/activate && python3 scripts/report_outcomes.py

backtest:
	@echo "Building historical scorecards from settled outcomes ..."
	@source .venv/bin/activate && python3 scripts/backtest_scorecards.py

tune-policy:
	@echo "Tuning policy thresholds from settled outcomes ..."
	@source .venv/bin/activate && python3 scripts/tune_policy.py

compute-cooccurrence:
	@echo "Computing phrase co-occurrence index from resolved outcomes ..."
	@source .venv/bin/activate && python3 scripts/compute_cooccurrence.py

fetch-signals:
	@echo "Fetching external signals (WH RSS, Google News, Truth Social, Trends) ..."
	@source .venv/bin/activate && python3 scripts/fetch_signals.py

analyze-signals:
	@echo "Running LLM reasoning pass (requires OPENAI_API_KEY) ..."
	@source .venv/bin/activate && python3 scripts/analyze_signals.py

signal-refresh:
	@echo "Full signal refresh: fetch + LLM analysis ..."
	@source .venv/bin/activate && python3 scripts/fetch_signals.py && python3 scripts/analyze_signals.py

install-24x7:
	@echo "Installing 24x7 launchd runner service ..."
	@source .venv/bin/activate && python3 scripts/manage_launchd.py install --repo-root .

install-24x7-all:
	@echo "Installing 24x7 launchd runner + dashboard services ..."
	@source .venv/bin/activate && python3 scripts/manage_launchd.py install --repo-root . --with-dashboard

uninstall-24x7:
	@echo "Uninstalling 24x7 launchd runner service ..."
	@source .venv/bin/activate && python3 scripts/manage_launchd.py uninstall

uninstall-24x7-all:
	@echo "Uninstalling 24x7 launchd runner + dashboard services ..."
	@source .venv/bin/activate && python3 scripts/manage_launchd.py uninstall --with-dashboard

restart-24x7:
	@bash scripts/restart_24x7.sh restart all

status-24x7:
	@bash scripts/restart_24x7.sh status all

local-up:
	@bash scripts/local_stack.sh start

local-up-fresh:
	@bash scripts/local_stack.sh fresh-start

local-down:
	@bash scripts/local_stack.sh stop

local-restart:
	@bash scripts/local_stack.sh restart

local-status:
	@bash scripts/local_stack.sh status

