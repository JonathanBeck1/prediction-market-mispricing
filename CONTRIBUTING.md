# Contributing

Thanks for your interest. Given the project's honest track record (see README), the most useful contributions are critique, analysis, and fixes — not additions.

## What's Most Useful

### High-impact
- **Why is MLB BUY_NO losing?** `bunt` has 19% WR on 17 bets, `-$5.94`. Something structural is wrong with how MLB phrase rates are calibrated. Data analysis welcome.
- **BayesianScorer NBA fix**: The NBA speaker prior (0.591) causes too many BUY_YES bets. A minimum CI exclusion margin (`yes_ask < ci_low - 0.05`) should help. Code PR welcome.
- **Linux/Docker support**: The 24/7 daemon is macOS-only (launchd). A `docker-compose.yml` + systemd unit file would make this runnable on Linux servers.
- **Alternative corpus sources**: We can't ship Trump/Leavitt transcripts due to copyright. If you know reliable, redistributable public-domain transcript sources, open an issue.

### Medium-impact
- **Backtesting with your own data**: If you have Kalshi trading history, running `scripts/backtest_outcomes.py` against it and sharing results helps validate whether the profitable segments are real or historical artifacts.
- **Test coverage**: `tests/test_openclaw_integration.py` is excluded from CI because it requires external deps. Could be converted to fully mocked tests.

### Low-impact (please skip)
- Adding new LLM signal layers (live data shows LLM signals are net-negative)
- Adding new scoring gates without evidence from `outcome_reviews` data
- New dashboards / visualizations

## Rules

1. **Run the test suite before submitting.** All 246 tests must pass: `python3 -m pytest -q`
2. **Any scoring gate change requires outcome data.** If you change `app/scoring.py` or `app/bayesian_scorer.py`, cite specific bet counts, win rates, and P&L from `outcome_reviews` in the PR description and append to `brain/08_DECISIONS_LOG.md`.
3. **No order placement.** The manual-only invariant is non-negotiable. PRs that add auto-execution will be closed.
4. **No secrets in PRs.** Double-check that `config/runtime.env` and any API keys are not committed.

## Setup

```bash
git clone https://github.com/yourusername/kalshi-edge.git
cd kalshi-edge
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 -m pytest -q   # verify baseline
```

## Opening Issues

For bugs: include the error traceback and relevant log lines from `data/logs/runner.err.log`.

For performance analysis: include the output of `make report-outcomes` and the specific market/phrase combination you're analyzing.
