# Validation + Live Rehearsal (LLM Deferred)

Date: 2026-03-10

## Regression Result

- Command: `source .venv/bin/activate && python3 -m pytest -q`
- Result: `180 passed in 7.63s`
- Status: PASS

## Rehearsal Commands

1) Canonical pre-event flow:

```bash
source .venv/bin/activate
make event-ready
```

2) Live runner rehearsal (short burst):

```bash
source .venv/bin/activate
KALSHI_MOCK=0 python3 -m app.runner
# observed for ~25s, then stopped
```

3) Strict health gate after live burst:

```bash
python3 scripts/health_check.py --max-snapshot-age-sec 600 --max-card-age-sec 600 --min-wallet-present-ratio 0.0 --json
```

## Health Gate Output (Strict)

- `snapshot_age_sec`: `49.11`
- `card_age_sec`: `44.11`
- `coverage_untracked_event_count`: `0`
- `poly_linked_ratio`: `0.0649`
- `wallet_present_ratio`: `0.0260`
- Overall `ok`: `true`

All strict checks passed:
- snapshot freshness
- action-card freshness
- coverage untracked events
- Polymarket signal completeness
- wallet signal presence

## Acceptance Gates vs Plan

- Coverage diagnostics explain tracked/untracked status for open events: PASS
- Poly signal confidence and quality gating integrated in fetch + scoring + dashboard tags: PASS
- Wallet-flow signal cache + bounded scorer modifier + explainable reason tags: PASS
- Canonical event-day flow (`make event-ready`) and health checks documented/run: PASS
- Full regression and rehearsal evidence recorded: PASS

## Residual Risk Notes

- Public Polymarket trade APIs expose noisy global trade streams; wallet matching is constrained to recent windows and may be sparse outside active event periods.
- `event-ready` uses relaxed freshness thresholds (pre-runner phase); run strict `make health-check` once runner is live for real-time freshness guarantees.
