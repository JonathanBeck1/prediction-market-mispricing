# Dashboard Screenshots

Screenshots referenced by the main [README](../../README.md).

## Required files

| Filename | Tab | What to capture |
|---|---|---|
| `markets.png` | Markets | Main view with speaker groups expanded showing BUY/WATCH cards |
| `sports.png` | Sports | NBA/NCAAB/MLB games list with phrase probabilities |
| `intelligence.png` | Intelligence | Signal analysis with at least one phrase row expanded |
| `performance.png` | Performance | P&L charts + speaker-level win rate table |
| `analysis.png` | Analysis | Co-occurrence or correlation view |
| `system.png` | System | Health panel with snapshot age, scorer idle, DB status |
| `scripts.png` | Scripts | Scripts list, ideally with one script running with output visible |

## How to take them

1. Make sure both runner and dashboard are running:
   ```bash
   make local-status
   ```
2. Open http://localhost:8777 in Chrome or Safari
3. For each tab, take a screenshot (⌘+Shift+4 on macOS, then space to capture the window)
4. Save to `docs/screenshots/<filename>.png`

## Guidelines

- **Resolution:** 1440×900 or larger. GitHub renders README images at ~600px wide so anything smaller loses detail.
- **Theme:** Use whatever the dashboard defaults to (dark).
- **Content:** Real data is fine — there are no secrets in the rendered UI. If a phrase or speaker feels too personal, you can redact it with a tool like CleanShot, but most users will find real data more compelling.
- **File size:** Keep each under ~500KB. Use PNG (not JPG) for UI screenshots — they compress better and stay sharp on Retina displays. `pngquant` can shrink them further if needed:
  ```bash
  brew install pngquant
  pngquant --quality=65-80 --output markets.png --force markets.png
  ```

## After adding images

```bash
git add docs/screenshots/*.png
git commit -m "docs: add dashboard screenshots"
git push
```
