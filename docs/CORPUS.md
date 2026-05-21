# Corpus Notes

The transcript corpus is intentionally not committed to this repository.

Some sources are public domain, while others are copyrighted or have unclear redistribution rights. Keep local transcript text under `data/corpus/`, which is gitignored.

## Layout

```text
data/corpus/<speaker>/<event_type>_YYYY-MM-DD_NN.txt
```

Examples:

```text
data/corpus/trump/rally_2026-02-20_01.txt
data/corpus/leavitt/briefing_2026-03-18_01.txt
data/corpus/powell/presser_2026-03-19_01.txt
```

Recognized event types include `rally`, `briefing`, `signing`, `presser`, `remarks`, `interview`, and `address`.

## Sources Used During Development

| Source | Typical use | Redistribution note |
|---|---|---|
| whitehouse.gov | Trump and White House press briefings | US government work, generally public domain |
| federalreserve.gov | Powell/FOMC press conferences | US government work, generally public domain |
| Rev.com | Rallies, interviews, and event transcripts | Do not redistribute without permission |
| factba.se | Trump historical transcript lookup | Check source terms before redistributing |
| C-SPAN | Mixed public events | Check source terms before redistributing |

## Local Commands

```bash
python3 scripts/add_transcript.py --speaker trump --event-type rally --date 2026-02-20
make ingest-corpus
python3 scripts/analyze_corpus.py
```

After adding meaningful corpus data, recompute the derived rates:

```bash
python3 scripts/compute_base_rates.py
python3 scripts/compute_rolling_rates.py
python3 scripts/compute_hazard_rates.py
```
