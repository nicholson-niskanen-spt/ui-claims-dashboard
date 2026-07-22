#!/usr/bin/env python3
"""
Fetch U.S. unemployment-insurance claims data from FRED and write data.json
for the dashboard.

- National series (weekly, back to 1967): initial & continued claims, SA & NSA.
- Recession bars: USREC -> intervals.
- State map: insured unemployment rate per state ({ST}INSUREDUR), latest value.

Design notes:
- stdlib only (urllib) so it runs in GitHub Actions with no pip install.
- Reads FRED_API_KEY from the environment.
- Shared QA runs on every pull (initial and scheduled). QA failure exits non-zero
  so GitHub Actions surfaces the failure.
- Every pull is stamped with fetched_at and copied to snapshots/ (git-tracked =
  free vintage history + recovery).
- Revision-diff compares the new pull against the most recent prior snapshot and
  flags any changed value for a week already treated as final.
"""

import json
import os
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime
from glob import glob

# On systems whose Python lacks a linked CA bundle (common with python.org
# installs on macOS), fall back to certifi's bundle. On Linux/CI the default
# context already works, so certifi is optional.
try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except Exception:  # noqa: BLE001
    SSL_CONTEXT = ssl.create_default_context()

FRED_BASE = "https://api.stlouisfed.org/fred/series/observations"
API_KEY = os.environ.get("FRED_API_KEY", "").strip()
HISTORY_START = "1967-01-01"

# Number of most-recent weekly points treated as provisional (subject to revision).
PROVISIONAL_WEEKS = 2

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(HERE, "data.json")
SNAP_DIR = os.path.join(HERE, "snapshots")

NATIONAL = {
    "ICSA":  "Initial Claims (Seasonally Adjusted)",
    "ICNSA": "Initial Claims (Not Seasonally Adjusted)",
    "CCSA":  "Continued Claims (Seasonally Adjusted)",
    "CCNSA": "Continued Claims (Not Seasonally Adjusted)",
}

STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI",
    "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN",
    "MS", "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH",
    "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA",
    "WV", "WI", "WY",
]


def fetch_observations(series_id, start=HISTORY_START, retries=3):
    """Return list of (date_str, value_or_None) for a FRED series."""
    params = urllib.parse.urlencode({
        "series_id": series_id,
        "observation_start": start,
        "file_type": "json",
        "api_key": API_KEY,
    })
    url = f"{FRED_BASE}?{params}"
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30, context=SSL_CONTEXT) as resp:
                payload = json.load(resp)
            out = []
            for obs in payload.get("observations", []):
                v = obs["value"]
                out.append((obs["date"], None if v in (".", "") else float(v)))
            return out
        except Exception as e:  # noqa: BLE001 - network/JSON errors, retry
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"failed to fetch {series_id}: {last_err}")


def usrec_to_intervals(obs):
    """Convert monthly 0/1 USREC observations into [start, end] recession spans."""
    intervals = []
    start = None
    prev_date = None
    for d, v in obs:
        if v == 1.0 and start is None:
            start = d
        elif v == 0.0 and start is not None:
            intervals.append([start, prev_date])
            start = None
        prev_date = d
    if start is not None:
        intervals.append([start, prev_date])
    return intervals


def latest_valid(obs):
    """Return (date, value) of the most recent non-null observation, or None."""
    for d, v in reversed(obs):
        if v is not None:
            return d, v
    return None


def run_qa(national):
    """Structural + sanity checks shared by initial and scheduled pulls.

    Returns (passed: bool, report: dict).
    """
    report = {}
    problems = []

    for sid in NATIONAL:
        obs = national[sid]
        valid = [(d, v) for d, v in obs if v is not None]
        if not valid:
            problems.append(f"{sid}: no valid observations")
            continue
        earliest, latest = valid[0][0], valid[-1][0]
        report[sid] = {
            "earliest": earliest,
            "latest": latest,
            "count": len(valid),
        }
        # Expect deep weekly history: ~52 weeks/yr since ~1967 -> a few thousand.
        if len(valid) < 2000:
            problems.append(f"{sid}: only {len(valid)} obs (expected >2000)")
        # Latest point should be recent (within ~40 days of today).
        latest_dt = datetime.strptime(latest, "%Y-%m-%d").date()
        age = (date.today() - latest_dt).days
        if age > 40:
            problems.append(f"{sid}: latest obs {latest} is {age} days stale")

    # 2020 spike sanity check on initial claims (peaked ~6M in spring 2020).
    icsa = dict(national["ICSA"])
    spike = [v for d, v in national["ICSA"]
             if v is not None and "2020-03-15" <= d <= "2020-05-01"]
    if not spike or max(spike) < 3_000_000:
        problems.append(
            "ICSA: spring-2020 spike check failed "
            f"(max={max(spike) if spike else 'n/a'}, expected >3,000,000)"
        )
    else:
        report["spike_check_2020"] = {"max": max(spike), "ok": True}

    return (len(problems) == 0), {"report": report, "problems": problems}


def revision_diff(new_national):
    """Compare new pull against the most recent prior snapshot.

    Flags any value change for a week older than the provisional window
    (i.e. a week that should already have been final).
    """
    snaps = sorted(glob(os.path.join(SNAP_DIR, "data-*.json")))
    if not snaps:
        return {"prior_snapshot": None, "changed_final_weeks": []}
    with open(snaps[-1]) as f:
        prior = json.load(f)
    prior_nat = prior.get("national", {})
    changed = []
    for sid in NATIONAL:
        new_obs = new_national[sid]
        old = dict(tuple(x) for x in prior_nat.get(sid, []))
        # Dates in the current pull that were final as of the prior snapshot:
        final_cutoff = [d for d, _ in new_obs][:-PROVISIONAL_WEEKS] \
            if len(new_obs) > PROVISIONAL_WEEKS else []
        final_set = set(final_cutoff)
        for d, v in new_obs:
            if d in final_set and d in old and old[d] is not None and v is not None:
                if abs(old[d] - v) > 0.5:
                    changed.append(
                        {"series": sid, "date": d, "old": old[d], "new": v}
                    )
    return {"prior_snapshot": os.path.basename(snaps[-1]),
            "changed_final_weeks": changed}


def main():
    if not API_KEY:
        print("ERROR: FRED_API_KEY not set in environment.", file=sys.stderr)
        sys.exit(2)

    print("Fetching national series...")
    national = {}
    for sid in NATIONAL:
        national[sid] = fetch_observations(sid)
        print(f"  {sid}: {len(national[sid])} obs")

    print("Fetching recession indicator (USREC)...")
    usrec = fetch_observations("USREC")
    recessions = usrec_to_intervals(usrec)

    print("Fetching state insured unemployment rates...")
    states = {}
    for st in STATES:
        try:
            obs = fetch_observations(f"{st}INSUREDUR", start="2015-01-01")
            lv = latest_valid(obs)
            if lv:
                states[st] = {"rate": lv[1], "date": lv[0]}
        except Exception as e:  # noqa: BLE001 - skip a missing state, keep going
            print(f"  WARN {st}INSUREDUR: {e}", file=sys.stderr)
    print(f"  {len(states)}/{len(STATES)} states with data")

    # QA gate.
    passed, qa = run_qa(national)
    print("\nQA report:")
    for sid, r in qa["report"].items():
        print(f"  {sid}: {r}")
    if qa["problems"]:
        print("QA PROBLEMS:")
        for p in qa["problems"]:
            print(f"  - {p}", file=sys.stderr)

    # Revision diff vs prior snapshot (informational; does not fail the run).
    rev = revision_diff(national)
    if rev["changed_final_weeks"]:
        print(f"\nNOTE: {len(rev['changed_final_weeks'])} final-week revision(s) "
              f"vs {rev['prior_snapshot']}:")
        for c in rev["changed_final_weeks"][:10]:
            print(f"  {c['series']} {c['date']}: {c['old']} -> {c['new']}")

    # Provisional weeks = last N dates of the SA initial-claims series.
    icsa_dates = [d for d, _ in national["ICSA"]]
    provisional = icsa_dates[-PROVISIONAL_WEEKS:] if icsa_dates else []

    data = {
        "fetched_at": date.today().isoformat(),
        "source": "U.S. Employment & Training Administration via FRED "
                  "(Federal Reserve Bank of St. Louis)",
        "series_labels": NATIONAL,
        "national": national,
        "recessions": recessions,
        "states": states,
        "provisional_weeks": provisional,
        "provisional_window": PROVISIONAL_WEEKS,
        "qa": qa,
        "revision_diff": rev,
    }

    with open(DATA_PATH, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    snap_path = os.path.join(SNAP_DIR, f"data-{data['fetched_at']}.json")
    with open(snap_path, "w") as f:
        json.dump(data, f, separators=(",", ":"))
    size_kb = os.path.getsize(DATA_PATH) / 1024
    print(f"\nWrote {DATA_PATH} ({size_kb:.0f} KB) and snapshot {os.path.basename(snap_path)}")

    if not passed:
        print("QA FAILED — exiting non-zero.", file=sys.stderr)
        sys.exit(1)
    print("QA passed.")


if __name__ == "__main__":
    main()
