# Project Brief: kalshi-mention-edge

## Purpose

Build a 24/7 manual-only edge engine for Kalshi political speaker mention markets. Covers Donald Trump, Karoline Leavitt, and Zohran Mamdani.

The system detects mispricing and timing edges across two trading windows:
1. **Pre-event**: take positions before speeches start, using historical base rates and external signals (X buzz, news pressure).
2. **During event**: monitor live transcripts for phrase hits and time-remaining decay, generating real-time BUY YES / BUY NO / WATCH alerts.

Outputs ranked Action Cards with evidence for manual trading decisions on your phone.

## Non-Negotiable Guardrails

- **Manual execution only.**
- **Never place orders.**
- **Never automate trading actions.**
- Output recommendations and evidence only.

## Edge Sources (Ranked by Impact)

1. **Speech-state edge**: "Event almost over, phrase hasn't appeared" = strong NO.
2. **Pre-event base rate edge**: historical rates suggest mispricing before speech starts.
3. **Literal-vs-topic mismatch**: crowd prices topic probability, but resolution is literal token.
4. **External signal edge**: X/news pressure shifts probability before the market adjusts.
5. **Stale repricing in thin markets**: reality moves faster than the book.

## Product Scope

- Deterministic Python services run continuously.
- Event model with scheduled/live/ended states and event-type conditioning.
- Composite scoring: base_rate * time_decay * news_pressure * x_buzz.
- Pluggable transcript ingestion with fallback ladder.
- Boundary-safe literal phrase matching.
- Liquidity-gated expected value scoring with three-way output (BUY YES / BUY NO / WATCH).
- Action Cards with executable price hints, size caps, reason codes, and evidence.
- WhatsApp delivery via OpenClaw (integration phase).

## Out of Scope

- Order routing, broker integration, auto-execution.
- Constant LLM polling or always-on agent loops.
- Rules-summary parsing (deferred).
- TrapIndex / substitution maps (deferred).

## Operating Principles

- **Local-first**: everything works on your Mac with mock data before any integration.
- **Mostly code**: deterministic loops, minimal AI.
- LLM/agent use is optional and event-driven only.
- OpenClaw is a data adapter and notification channel, not the math engine.
- System must continue running if any source fails.
- All decisions and evidence must be inspectable from stored data.

## Success Criteria

- Pre-event cards with correct base rates for scheduled events.
- Time-decay NO alerts during live events with no phrase hit.
- Instant BUY_YES confirmation when phrase detected.
- Clean, phone-scannable action cards via WhatsApp.
- Stable 24/7 runtime under partial source failures.
- Low operating cost via caching and event-driven invocation.
- `brain/` docs remain the source of truth for all implementation.
