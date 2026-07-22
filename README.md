# U.S. Unemployment Insurance Claims Dashboard

A clean, self-contained dashboard of U.S. unemployment-insurance (UI) claims,
backed by FRED data from **January 1967** to present, with an automated weekly
refresh after each Thursday DOL release.

- **National trends** — initial & continued claims, seasonally adjusted (SA) and
  not (NSA), back to 1967, with a time-frame control, recession shading, an
  honest trend readout, and a log-scale option to tame the 2020 spike.
- **State map** — a tile cartogram of the **insured unemployment rate** by state
  (continued claims as a share of covered employment), the standard cross-state
  comparable. Equal-area tiles so color reflects the rate, not state size.
- The most recent **2 weeks are marked provisional** (routinely revised).

## How it works

```
fetch_data.py   → pulls FRED series, runs QA, writes data.json + a dated snapshot
data.json       → the single file the dashboard reads
snapshots/      → dated vintage copies (git-tracked = free history + recovery)
index.html      → the dashboard (inline SVG charts, no external libraries)
.github/workflows/update.yml → weekly GitHub Actions job that refreshes data.json
```

### Data & QA
- National: `ICSA`, `ICNSA`, `CCSA`, `CCNSA`; `USREC` for recession bars.
- State: `{STATE}INSUREDUR` (insured unemployment rate), latest value per state.
- Each pull is QA-gated (earliest/latest date, row count, a spring-2020 spike
  sanity check) and stamped with its access date. QA failure exits non-zero so
  the scheduled job fails loudly. A revision-diff flags any change to a week that
  was already final in the prior snapshot.
- Release schedule: **Thursdays 8:30am ET**, shifting on federal holidays
  ([DOL](https://www.dol.gov/ui/data.pdf)).

## Run locally

```bash
export FRED_API_KEY=your_key   # free key: https://fredaccount.stlouisfed.org/apikeys
python3 fetch_data.py          # writes data.json
python3 -m http.server 8765    # then open http://localhost:8765
```

## Deploy the weekly auto-update (GitHub Actions)

1. Create a GitHub repo and push this folder (commands below).
2. In the repo: **Settings → Secrets and variables → Actions → New repository
   secret**, name it `FRED_API_KEY`, paste your key.
3. **Settings → Actions → General →** allow workflows to run.
4. (Optional) **Settings → Pages →** deploy from the `main` branch to host the
   dashboard at a public URL.

The workflow runs Thursdays/Fridays, refetches, and commits `data.json` only if
the data changed. Trigger a manual run any time from the **Actions** tab
("Weekly UI claims update" → Run workflow).

## Source
U.S. Employment & Training Administration via FRED (Federal Reserve Bank of
St. Louis).
