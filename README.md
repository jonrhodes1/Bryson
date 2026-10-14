# Performance Lab

A private readiness, peaking and taper dashboard built on your TrainingPeaks data.
It is a static GitHub Pages site, rebuilt every night by GitHub Actions.

```
TrainingPeaks ──(nightly, read-only)──▶ GitHub Action ──▶ analyse ──▶ encrypt ──▶ GitHub Pages
                                             ▲                                        │
                                  state.enc (encrypted check-ins) ◀── your browser ◀──┘ (decrypts locally)
```

## What is public and what is not

| Item | Where it lives | Readable by others? |
|---|---|---|
| Code (this repo) | GitHub | Yes, and it contains no data |
| `data.enc` (all training and health data) | Pages deployment only, never committed | Ciphertext only (AES-256-GCM) |
| `state.enc` (check-ins, race tags, settings) | Committed to this repo | Ciphertext only |
| `crypto.json` (salt and iteration count) | Pages | Yes, by design; it is not secret |
| TrainingPeaks cookie | GitHub Secret `TP_AUTH_COOKIE` | No |
| Passphrase | GitHub Secret `DASH_PASSPHRASE` and your head | No |

Because the ciphertext is public, **the passphrase is the only thing protecting it**. Use five or more random words
(e.g. `lantern-quartz-otter-meadow-viking`), not a reused password. The key is derived with PBKDF2-SHA256 at 600,000 iterations.

The sync only reads from TrainingPeaks; it never writes to it. It uses
[trainingpeaks-mcp](https://github.com/JamsusMaximus/trainingpeaks-mcp), pinned to a reviewed commit in `sync/requirements.txt`.

## One-off setup (about 10 minutes)

1. **Pages:** in this repo, go to *Settings → Pages → Build and deployment → Source* and choose **GitHub Actions**.
2. **Passphrase:** go to *Settings → Secrets and variables → Actions → New repository secret*. Name it `DASH_PASSPHRASE` and set your passphrase as the value.
3. **TrainingPeaks cookie:**
   1. Log in at <https://app.trainingpeaks.com> in Chrome or Safari.
   2. Open DevTools (Chrome: ⌥⌘I → *Application → Cookies*; Safari: enable the Develop menu, then *Storage → Cookies*).
   3. Copy the value of **`Production_tpAuth`**.
   4. Add it as a new repository secret named `TP_AUTH_COOKIE`.

   Treat it like a password: paste it only into GitHub Secrets.
4. **Optional variables** (*Secrets and variables → Actions → Variables*):
   - `TP_ATHLETE`: the athlete to sync on a coach account. It defaults to `Bryson Kay`; use the athlete ID if the name is ambiguous.
   - `TP_START`: the first date to pull, e.g. `2024-01-01`. The default is two years back.
5. **First run:** go to *Actions → Sync → Run workflow*. The first run back-fills your history and takes 10 to 20 minutes. After that it runs nightly at 04:15 UTC, and also whenever you save a check-in.
6. **Open** `https://jonrhodes1.github.io/Bryson/` and unlock it with your passphrase.
7. **Connect check-ins:** create a [fine-grained token](https://github.com/settings/personal-access-tokens/new):
   - Repository access: *Only select repositories → Bryson*
   - Permissions: *Contents → Read and write*, and nothing else
   - Expiry: 1 year

   In the dashboard, open *Settings (gear) → GitHub connection* and paste the token. It is stored on that device only, encrypted with your passphrase. Repeat on each device you use.

## Daily use

- **Check-in** (60 seconds, ideally on waking): seven 1 to 7 ratings, plus sleep, rMSSD and resting HR if your watch does not already send them.
- **Races:** add them to the TrainingPeaks calendar as A/B/C events, put `#race` in the workout title, or tag the session in *Activities*.
- **Tests:** every second week, run 15 minutes easy, then 3 km steady at a fixed effort on the same flat loop or treadmill, wearing a chest strap. Put `#test` in the title. After 20 tests, the peaking model fits to these instead of to day-to-day efficiency.
- **HRV, resting HR and sleep:** enable health-data sync from Garmin, Oura or Whoop to TrainingPeaks. These then appear on the Readiness tab automatically.

## When something breaks

- **The sync fails with `AUTH:` in the log, or the dashboard shows "Last sync … ago".** The TrainingPeaks cookie has expired, which happens every few weeks. Repeat setup step 3, then re-run the workflow.
- **Scheduled runs stop.** GitHub pauses schedules on repos with no commits for 60 days. Check-ins create commits, so this only happens if you stop checking in. Re-enable the workflow from the Actions tab.
- **Changing the passphrase.** Old `state.enc` cannot be read with a new key. Export your check-ins as CSV first (Check-in tab), then delete `state.enc`, update the secret, re-run the workflow, and re-import the CSV.

## Running locally (optional)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r sync/requirements.txt
export TP_AUTH_COOKIE='…' DASH_PASSPHRASE='…'
python sync/run.py --out _site && python -m http.server -d _site 8000
```

## Method notes

- **Load:** TrainingPeaks TSS where available. Otherwise Banister TRIMP normalised so that 1 h at threshold HR = 100, or a duration-based estimate when there is no HR.
- **Fitness / fatigue / form:** 42-day and 7-day exponentially weighted load. Form (TSB) is yesterday's fitness minus fatigue.
- **HRV:** ln rMSSD 7-day mean against a 60-day baseline, with the normal range set at ±0.5 SD.
- **Pace:** grade-adjusted pace uses the Minetti (2002) energy-cost polynomial.
- **Readiness:** weighted composite of form, acute:chronic ratio, monotony, recent load, HRV, resting HR, sleep and check-in, renormalised over the signals that are present. The weights are provisional.
- **Peaking model:** Banister impulse-response fitted to test results (from 20 tests) or to steady-run efficiency. It is only used when it beats a linear trend and the mean on held-out data.
- **Taper simulator:** the evidence band is 8 to 14 days with a 41 to 60% progressive volume cut, intensity held (Bosquet et al., 2007).
