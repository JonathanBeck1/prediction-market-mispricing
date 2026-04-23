# Quickstart — See It Running in 10 Minutes

This guide gets the system running in **mock mode** (no real API, no real money) so you can explore it locally.

## Prerequisites

- macOS or Linux
- Python 3.9+
- Git

## Step 1: Clone and Install

```bash
git clone https://github.com/yourusername/kalshi-edge.git
cd kalshi-edge

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Step 2: Verify Tests Pass

```bash
python3 -m pytest -q
# Expected: 246 passed
```

## Step 3: Start the Runner (Mock Mode)

Mock mode uses deterministic fake market prices instead of the Kalshi API. Safe to run, generates ~600 fake markets with realistic prices.

```bash
KALSHI_MOCK=1 python3 -m app.runner
```

You'll see output like:
```
INFO  [__main__] Using BayesianScorer (hierarchical Beta-Binomial mispricing detector)
INFO  [__main__] Watchdog enabled (snapshot_age<=300s scorer_idle<=300s).
INFO  [__main__] Starting services. mode=MOCK ...
INFO  [app.kalshi_watcher] Mock watcher: 661 snapshots written
```

## Step 4: Start the Dashboard

In a second terminal:

```bash
source .venv/bin/activate
python3 -m app.dashboard
```

Open **http://localhost:8777** in your browser.

### What You'll See

- **Action Cards tab**: Live BUY / WATCH signals from the scoring model
- **Performance tab**: P&L history and win rates by segment (empty until you run with real data)
- **Intelligence tab**: Model diagnostics, signal weights, phrase analysis
- **Sports tab**: NBA/NCAAB/MLB/MMA market status

In mock mode, the watcher generates stable deterministic prices. The scorer will emit BUY/WATCH signals based on those prices against calibrated phrase rates.

## Step 5: Try Live Mode (Optional)

For real Kalshi market prices (still advisory-only, still no order placement):

```bash
# Copy and edit the config template
cp config/runtime.env.example config/runtime.env
# Edit config/runtime.env:
#   KALSHI_MOCK=0
#   (all API keys are optional — basic price polling uses the public API)

python3 -m app.runner
```

Kalshi's public API allows up to 20 reads/second without authentication. No API key is required for basic market polling.

## Step 6: Fetch Real Data

```bash
# Fetch current market definitions (~700 markets)
make fetch-markets

# Fetch resolved outcomes for calibration
make fetch-outcomes

# Fetch Polymarket cross-prices (optional signal)
make fetch-poly
```

## Step 7: Toggle the Scorer

Try switching between the two scoring models:

```bash
# Bayesian (default) — pure statistics, no LLM cost
KALSHI_MOCK=1 USE_BAYESIAN_SCORER=1 python3 -m app.runner

# Legacy ScoringEngine — LLM-enhanced (requires OPENAI_API_KEY in runtime.env)
KALSHI_MOCK=1 USE_BAYESIAN_SCORER=0 python3 -m app.runner
```

## 24/7 Operation (macOS)

### Before you install: macOS privacy permissions

This is the part that trips most people up. macOS has several security layers that can silently block background services. Do these steps **before** running `make install-24x7-all`.

**1. Remove the quarantine flag from the repo**

When you clone from GitHub, macOS tags downloaded files with a quarantine attribute. This can cause launchd to refuse to execute the shell scripts. Remove it:

```bash
xattr -dr com.apple.quarantine /path/to/kalshi-edge
```

You'll need to re-run this if you do a fresh clone.

**2. Give Terminal Full Disk Access**

Open **System Settings → Privacy & Security → Full Disk Access** and make sure **Terminal** is toggled on. If you use iTerm2 or another terminal emulator, add that too.

This is required because the launchd process inherits Terminal's sandbox level. Without Full Disk Access, the runner can silently fail to read files in certain directories — you'll see the process start and immediately exit with no obvious error.

**3. Keep the repo out of Documents/Desktop/Downloads**

macOS applies stricter privacy controls to files under `~/Documents`, `~/Desktop`, and `~/Downloads`. If your repo lives there, launchd services may be blocked from reading the `.venv` folder.

Recommended location: `~/kalshi-edge` or anywhere directly under your home directory.

```bash
# If you cloned to ~/Documents/kalshi-edge, move it:
mv ~/Documents/kalshi-edge ~/kalshi-edge
cd ~/kalshi-edge
```

**4. Allow the scripts to run (Gatekeeper)**

The first time launchd tries to run the shell scripts, macOS may show a Gatekeeper popup or silently block them. If the service loads but immediately exits (check `launchctl list | grep kalshi` — a non-zero LastExitStatus means it's crashing on launch):

```bash
# Check what's happening
tail -50 data/logs/runner.err.log

# If the script was blocked, approve it via:
spctl --add scripts/launchd_run_runner.sh
spctl --add scripts/watchdog.sh
```

Or just open each `.sh` file in Finder (right-click → Open) once to approve it through the normal Gatekeeper flow.

**5. Prevent macOS from sleeping**

The runner needs the Mac to stay awake to poll prices. The install script sets up a `caffeinate` service automatically, but you can also enable **System Settings → Battery → Prevent automatic sleeping when display is off** for a permanent solution.

---

### Install the services

Once the above is done:

```bash
# Install as launchd services
make install-24x7-all

# Check status
make local-status

# View logs
tail -f data/logs/runner.err.log
```

To stop:
```bash
make uninstall-24x7-all
```

## Understanding the Output

### Action Cards

Each card looks like:
```
[BUY_NO] | KXNBAMENTION-26APR18ATLNYK-ALLE | "alley-oop"
  p_model=0.386  ci=[0.316, 0.455]  market_yes_ask=0.55
  ev_no=+0.064  kelly=0.127  size_rec=$10
  reasons: BAYESIAN_V1, CI_EXCLUDES_MARKET_NO, THICK_DATA, GATE_PASS
```

- **p_model**: Posterior mean YES probability from our Bayesian model
- **ci**: 90% credible interval — market price (0.55) is above ci_high (0.455), so YES is overpriced
- **ev_no**: Expected value of the NO bet
- **reasons**: Gate codes explaining why the card was generated

### Reason Codes

| Code | Meaning |
|------|---------|
| `BAYESIAN_V1` | Generated by the new BayesianScorer |
| `CI_EXCLUDES_MARKET_YES` | Market price < ci_low → market underprices YES |
| `CI_EXCLUDES_MARKET_NO` | Market price > ci_high → market overprices YES |
| `THICK_DATA` | ≥10 historical observations for this phrase |
| `NO_PHRASE_DATA` | No historical data — using speaker prior only |
| `KELLY_WEAK` | Edge too small for Kelly minimum threshold |
| `GLOBAL_YES_BLOCK` | Legacy scorer: all BUY_YES currently blocked |

## Troubleshooting

**"Another runner instance is already running"**
```bash
rm data/runner.lock
```

**"database disk image is malformed"**
```bash
rm -f data/edge.db-shm data/edge.db-wal
python3 -m app.runner   # will auto-heal on startup
```

**Dashboard shows blank / API errors**
```bash
# Runner and dashboard must both be running
# Check runner logs:
tail -50 data/logs/runner.err.log
```

**launchd service loads but immediately exits (LastExitStatus != 0)**
```bash
# Check the actual error
tail -50 data/logs/runner.err.log

# Most common causes:
# 1. Missing Full Disk Access for Terminal — see macOS Privacy section above
# 2. Quarantine flag on scripts — run: xattr -dr com.apple.quarantine .
# 3. Repo is in ~/Documents or ~/Desktop — move it to ~/kalshi-edge
# 4. .venv doesn't exist — run: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

**"Operation not permitted" or "Permission denied" in logs**
```bash
# This is almost always the Full Disk Access issue on macOS.
# System Settings → Privacy & Security → Full Disk Access → enable Terminal
```

**Mac goes to sleep and runner stops**

The install script creates a `caffeinate` service automatically. If your Mac is still sleeping:
```bash
# Check if caffeinate is running
launchctl list com.kalshi-edge.caffeinate

# Or enable permanent setting:
# System Settings → Battery → Options → Prevent automatic sleeping when display is off
```

**Script blocked by Gatekeeper on first run**
```bash
# If you see "cannot be opened because the developer cannot be verified":
# Option 1: right-click the .sh file in Finder and choose Open
# Option 2: approve via spctl:
spctl --add scripts/launchd_run_runner.sh
spctl --add scripts/watchdog.sh

# Or disable Gatekeeper check for this repo (less safe):
xattr -dr com.apple.quarantine .
```
