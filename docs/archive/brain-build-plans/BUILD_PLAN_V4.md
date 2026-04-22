# Build Plan V4 — Model Accuracy Overhaul

**Context**: Live BSS is negative (-0.36) despite positive walk-forward BSS (+0.13), indicating systematic calibration issues. Recent analysis of 374 outcomes shows massive underestimation: model predicts <20% but actual YES rate is 41.5%.

**Goal**: Fix the model's systematic bias and improve live performance to match or exceed walk-forward BSS.

---

## **Phase 1: Critical Calibration Fixes (2-3 hours) — ROOT CAUSE**

### **P1.1: Speaker-Stratified Global Defaults [CRITICAL - 1 hour]**

**Problem**: `_global_default = 0.48` used for unknown phrases is systematically too low for all speakers.

**Evidence**: Massive underestimation by speaker:
- NBA: Model 13.8% → Actual 47.1% (3.4× gap)
- NCAAB: Model 15.0% → Actual 41.8% (2.8× gap)  
- Trump: Model 7.6% → Actual 48.0% (6.3× gap)
- Auto: Model 12.9% → Actual 44.7% (3.5× gap)

**Implementation**:
1. `app/base_rates.py`: Add speaker-specific global defaults:
   ```python
   _SPEAKER_DEFAULTS = {
       "nba": 0.65,    "ncaab": 0.58,   "trump": 0.55,   "auto": 0.55,
       "leavitt": 0.45, "mma": 0.45,    "mlb": 0.35,     # Based on actual data
   }
   ```
2. Update `BaseRateLookup._context_prior()` to use speaker-specific defaults
3. Update `config/base_rates.yaml` `_global_default` entries per speaker

**Files**: `app/base_rates.py`, `config/base_rates.yaml`  
**Tests**: Update `test_empty_data` assertions  
**Expected**: Fix 50-80% of the BSS gap immediately

---

### **P1.2: Platt Calibration Sample Size Fix [CRITICAL - 30 mins]**

**Problem**: 374 outcomes < 400 minimum for reliable Platt fitting. System falls back to identity calibration but should use a simpler correction.

**Evidence**: `_MIN_SAMPLES = 400` but we only have 374 outcomes.

**Implementation**:
1. `app/calibration.py`: Lower `_MIN_SAMPLES` from 400 → 200 for basic correction
2. Add sample size warning when 200 ≤ N < 400  
3. Use simple linear correction when N < 200: `p_cal = 0.3 + 0.7 * p_literal`

**Files**: `app/calibration.py`  
**Expected**: Enable calibration on current sample size

---

### **P1.3: Event-Type Stratified Calibration [HIGH - 2 hours]**

**Problem**: Different event types have different systematic biases. `briefing` vs `rally` vs `sports` should use separate calibrators.

**Implementation**:
1. `app/calibration.py`: Add event-type stratification alongside speaker stratification
2. New strata: `briefing_trump`, `briefing_leavitt`, `rally_trump`, `sports_nba`, etc.
3. Fallback chain: specific → speaker → global
4. Update `ScoringEngine` to use event-type-aware calibration

**Files**: `app/calibration.py`, `app/scoring.py`  
**Expected**: 10-20pp win rate improvement for sports and briefings

---

## **Phase 2: Signal Quality Improvements (2-3 hours)**

### **P2.1: Enhanced Signal Double-Counting Fix [HIGH - 1 hour]**

**Problem**: Current `DOUBLE_COUNT_DAMPENED` is too narrow. Subtle correlations still compound.

**Implementation**:
1. `app/scoring.py`: Expand double-count detection:
   - When `news_pressure > 1.1` AND `llm_boost > 1.1`: dampen weaker by 50%
   - When `event_llm` and `llm_boost` same direction: cap combined at 2.0×
   - When `BIAS_MAP_RATE` + any other signal: cap total at 1.8×
2. Add `SIGNAL_CAPPED` reason code for audit trails

**Files**: `app/scoring.py`  
**Expected**: 5-10pp win rate improvement by reducing over-boosted signals

---

### **P2.2: LLM Signal Quality Gating [MEDIUM - 1 hour]**

**Problem**: LLM boost shows 50% WR vs suppress 56.2% WR. Boost is underperforming.

**Implementation**:
1. `app/scoring.py`: Add LLM confidence gating:
   - Only apply `llm_boost > 1.1` when `llm_confidence >= 0.7`
   - Cap `llm_boost` at 1.05× for `llm_confidence < 0.5`
2. Add `LLM_LOW_CONF_CAPPED` reason code

**Files**: `app/scoring.py`, `app/signals.py`  
**Expected**: Improve LLM signal win rate from 50% → 60%+

---

### **P2.3: Smart Reason Code Retirement [MEDIUM - 45 mins]**

**Problem**: Some reason codes consistently lose money but we keep using them.

**Implementation**:
1. `scripts/analyze_reason_performance.py`: Analyze win rate per reason code
2. Auto-flag reason codes with <30% WR over 20+ bets  
3. Add `DEPRECATED_SIGNAL` warning in scorer when flagged codes appear
4. Dashboard alert for deprecated signals

**Files**: New script + `app/scoring.py`  
**Expected**: Identify and deprecate 3-5 losing signal types

---

## **Phase 3: Advanced Model Architecture (3-4 hours)**

### **P3.1: Pre-Event vs Live Strategy Split [HIGH - 2 hours]**

**Problem**: Pre-event and live have fundamentally different dynamics but use same thresholds.

**Evidence**: Live bets 53% WR vs pre-event 18% WR in brain notes.

**Implementation**:
1. `app/scoring.py`: Separate EV thresholds:
   ```python
   _PRE_EVENT_EV_THRESHOLD = 0.15    # Higher threshold for pre-event
   _LIVE_EV_THRESHOLD = 0.08         # Lower threshold for live (faster exit)
   ```
2. Separate gate logic for key gates:
   - `PRE_EVENT_YES_BLOCK`: Block BUY_YES when `yes_ask < 0.40` pre-event
   - `PRE_EVENT_NO_CONSERVATIVE`: Higher NO conviction required pre-event
3. Add `PRE_EVENT_STRATEGY` / `LIVE_STRATEGY` reason codes

**Files**: `app/scoring.py`  
**Expected**: 15-25pp win rate improvement for pre-event bets

---

### **P3.2: Confidence-Based Early Exit [MEDIUM - 1 hour]**

**Problem**: We process low-confidence bets through 17+ gates when early exit would save computation and reduce noise.

**Implementation**:
1. `app/scoring.py`: Add early exit after initial signal calculation:
   ```python
   if score_confidence < 0.40 and not any_high_signals:
       return "WATCH", ["LOW_CONFIDENCE_SKIP"], components
   ```
2. High signals: `BIAS_MAP_RATE`, `PHRASE_HIT`, `CAUSAL_OVERRIDE`, `SCORE_CONF_HIGH`
3. Add `CONFIDENCE_EARLY_EXIT` reason code

**Files**: `app/scoring.py`  
**Expected**: 10-15% reduction in marginal bets, focus on high-conviction opportunities

---

### **P3.3: Rolling Rate Staleness Detection [MEDIUM - 1 hour]**

**Problem**: Rolling rates based on old transcript data may be stale for current events.

**Implementation**:
1. `app/rolling_rates.py`: Add staleness detection:
   - Check `last_seen` date in rolling data vs current date
   - Flag `ROLLING_STALE` when >21 days old
   - Auto-fallback to historical base rate when stale
2. `scripts/compute_rolling_rates.py`: Add data quality metrics

**Files**: `app/rolling_rates.py`, `scripts/compute_rolling_rates.py`  
**Expected**: Prevent stale rolling signals from degrading accuracy

---

## **Phase 4: Advanced Features (2-3 hours)**

### **P4.1: Market Regime Detection [MEDIUM - 1.5 hours]**

**Problem**: Some patterns consistently lose but we haven't systematized detection.

**Implementation**:
1. `scripts/detect_losing_patterns.py`: Auto-analyze win rates by:
   - Reason code combinations  
   - Price range × speaker combinations
   - Signal pattern combinations
2. `app/scoring.py`: Add `REGIME_POOR_PERFORMANCE` gate for patterns with <25% WR
3. Dashboard alerts for regime changes

**Files**: New script + `app/scoring.py` + dashboard  
**Expected**: Identify and block 5-10 systematic losing patterns

---

### **P4.2: Cross-Market Arbitrage Enhancement [MEDIUM - 1 hour]**

**Problem**: We have basic Polymarket integration but miss sophisticated arbitrage opportunities.

**Implementation**:
1. `app/polymarket.py`: Add arbitrage opportunity scoring:
   - Multi-leg arbitrage detection (Kalshi YES + Poly NO)
   - Time-to-expiry arbitrage (short-term vs long-term contracts)
2. Add `ARBITRAGE_OPPORTUNITY` signals with confidence scoring

**Files**: `app/polymarket.py`, `app/scoring.py`  
**Expected**: 3-5 additional high-confidence opportunities per day

---

### **P4.3: Dynamic Threshold Auto-Tuning [ADVANCED - 3 hours]**

**Problem**: Static thresholds (EV_MIN=0.10, Kelly 5%, etc.) may not be optimal across all market conditions.

**Implementation**:
1. `scripts/optimize_thresholds.py`: Grid search on historical data:
   - Sweep EV thresholds (0.05-0.20)  
   - Sweep Kelly thresholds (3%-10%)
   - Sweep confidence thresholds (0.5-0.8)
2. Find Pareto-optimal combinations (maximize WR and ROI simultaneously)
3. `config/adaptive_thresholds.yaml` with regime-specific settings

**Files**: New optimization script + config  
**Expected**: 5-15% improvement in threshold optimization

---

## **Phase 5: Data Pipeline Enhancements (3-4 hours)**

### **P5.1: Truth Social Real-Time Integration [HIGH - 2 hours]**

**Problem**: Trump posts phrases 2-6h before speeches but we only get them manually.

**Implementation**:
1. `scripts/fetch_truth_social_auto.py`: Real RSS scraping from truthsocial.com
2. Phrase extraction and boost calculation  
3. 15-minute auto-refresh pipeline
4. Integration with existing `process_truth_social.py`

**Files**: New script + pipeline integration  
**Expected**: 2-4 hour advance signal on Trump phrase probability

---

### **P5.2: Fed/Powell Corpus Auto-Expansion [MEDIUM - 2 hours]**

**Problem**: Powell has thin corpus but Fed events are highly formulaic and predictable.

**Implementation**:
1. `scripts/fetch_fed_transcripts.py`: Auto-fetch from federalreserve.gov
2. FOMC statement parsing and phrase extraction
3. Rate decision keyword boosting ("pause", "data dependent", "soft landing")

**Files**: Enhanced Fed pipeline  
**Expected**: Unlock Fed/Powell market accuracy

---

## **Implementation Priority Matrix**

| Phase | Task | Impact | Effort | Dependencies | ROI Score |
|-------|------|--------|--------|--------------|-----------|
| P1.1 | Speaker defaults | **CRITICAL** | 1h | None | **10/10** |
| P1.2 | Sample size fix | **CRITICAL** | 0.5h | None | **9/10** |
| P2.1 | Double-count fix | HIGH | 1h | None | **8/10** |
| P1.3 | Event stratification | HIGH | 2h | P1.1 complete | **8/10** |
| P3.1 | Pre-event/live split | HIGH | 2h | P1.1, P1.2 | **7/10** |
| P2.2 | LLM confidence | HIGH | 1h | None | **7/10** |
| P4.1 | Regime detection | MEDIUM | 1.5h | P1 complete | **6/10** |
| P3.2 | Early exit | MEDIUM | 1h | None | **6/10** |
| P5.1 | Truth Social auto | HIGH | 2h | External APIs | **6/10** |

---

## **Recommended 3-Session Plan**

### **Session 1: Core Calibration (3 hours)**
**Focus**: Fix the negative BSS root cause
- P1.1: Speaker-stratified defaults (1h)
- P1.2: Sample size fix (30m) 
- P2.1: Enhanced double-counting (1h)
- P1.3: Event-type stratification (2h)
- **Target**: BSS -0.36 → +0.10+

### **Session 2: Strategy Optimization (3 hours)**  
**Focus**: Improve win rate and reduce noise
- P3.1: Pre-event/live strategy split (2h)
- P2.2: LLM confidence gating (1h)
- P3.2: Confidence early exit (1h)
- **Target**: Win rate 56% → 65%+

### **Session 3: Advanced Features (4 hours)**
**Focus**: Systematic pattern detection and automation  
- P4.1: Market regime detection (1.5h)
- P5.1: Truth Social automation (2h)
- P4.2: Arbitrage enhancement (1h)  
- P3.3: Rolling staleness (1h)
- **Target**: Additional 5-10 high-conviction opportunities/day

---

## **Success Metrics**

**After Session 1** (Core fixes):
- [ ] BSS (30d live) > 0.0 (currently -0.36)
- [ ] Model calibration: actual YES rate within 10pp of predicted across all buckets
- [ ] Sports prediction accuracy >90% for structural certainties

**After Session 2** (Strategy optimization):
- [ ] Overall win rate >60% (currently 56.2%)
- [ ] BUY_YES win rate >50% (currently 41%)
- [ ] Pre-event strategy clearly differentiated from live strategy

**After Session 3** (Advanced features):  
- [ ] Auto-detection of ≥3 losing patterns with regime alerts
- [ ] Truth Social signal providing 2-4h advance notice on Trump phrases
- [ ] Cross-market arbitrage opportunities identified and flagged

---

## **Quick Start (Next 20 minutes)**

**Immediate high-value fix** — Speaker defaults adjustment:

```bash
# Test the theory with a quick simulation
python3 -c "
import sqlite3
conn = sqlite3.connect('data/edge.db')
# Count how many recent outcomes used global default fallback
# vs speaker-specific rates from base_rates.yaml
query = '''
SELECT speaker, COUNT(*) n,
  ROUND(100.0*AVG(p_literal),1) avg_model,
  ROUND(100.0*SUM(CASE WHEN outcome=\"yes\" THEN 1 ELSE 0 END)/COUNT(*),1) actual
FROM outcome_reviews  
WHERE resolved_ts > datetime(\"now\", \"-30 days\")
GROUP BY speaker HAVING COUNT(*) >= 10;
'''
for row in conn.execute(query):
    print(f'{row[0]:12s}: model={row[2]:5.1f}%  actual={row[3]:5.1f}%  gap={row[3]-row[2]:+5.1f}pp')
"
```

**If gaps are >20pp for any speaker, start with P1.1 immediately.**

---

## **Implementation Notes**

- All changes must preserve the 17-gate ordering in `scoring.py`  
- Update `brain/08_DECISIONS_LOG.md` after each phase with evidence and P&L impact
- Run full test suite after each major change: `python3 -m pytest -q`
- Validate BSS improvement after Phase 1: `python3 scripts/backtest_outcomes.py --live --save`

**Start with Phase 1?** It directly addresses the root cause of negative BSS.