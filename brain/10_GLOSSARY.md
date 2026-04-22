# Glossary

## Action Card

Ranked advisory output containing market context, evidence, scores, and rationale for manual decision-making.

## Manual-Only

Operating mode where the system never places orders and never executes trades automatically.

## Mention Market

Market resolved by whether a target speaker literally mentions a target term/topic in the relevant window.

## Phrase Hit

Boundary-safe literal match of a tracked phrase in transcript text.

## Evidence Snippet

Bounded text segment around a phrase hit, stored with offsets and source metadata.

## Transcript Source

Adapter that fetches transcript/caption text (for example `directhttp`, `openclaw`, `relay`).

## Fallback Ladder

Ordered source attempts used when retrieval fails at prior stages.

## Event ID

Stable key representing one speech/event window for a speaker.

## Speech State

Lifecycle state for an event:
- `live`
- `ended`
- `unknown`

## Liquidity Gates

Hard filters on spread and depth used to suppress weak markets before ranking.

## p_literal

Literal-mention probability heuristic based on whether required phrase evidence exists in the relevant period.

## EV (Expected Value)

Edge metric comparing probability estimate to market price:
- `ev_yes = p_yes - yes_ask`
- `ev_no = p_no - no_ask`

## Top-N

Operator-facing subset of highest-ranked Action Cards in a cycle.

## Dedupe

Suppression of repeated records/alerts using hashes, keys, and cooldown logic.

