# Handoff — paste this to start a new chat

I'm continuing work on **Gambling Host Tracker** at
`D:\Projects\Personal\Gambling Host Tracker` (git repo, pushed to
`github.com/NasirSunny50/Gambling-Host-Tracker`, branch `main`, all work committed).

Read `README.md` first — it covers the architecture. Read code only as you need it; don't
survey the whole tree.

## What it does

Collects the mobile-wallet and bank account numbers gambling sites publish for deposits, so
a bank's AML team can blocklist them. Numbers rotate daily, so it keeps every sighting and
stores the page it came from, hashed, as evidence. One target so far: `1xbet-bd`.

## How to run things

```
.venv\Scripts\python.exe -m pytest -q         # 230 tests, all offline
.venv\Scripts\python.exe -m ruff check src tests
scripts\Start.bat                             # portal on http://127.0.0.1:8000
```

Always use `.venv\Scripts\python.exe`, never the system Python. Kill stale servers before
restarting or the port stays bound and you'll test old code:
`Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'ght\.cli' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }`

Note: every `ght.cli` command shows as **two** python processes (a parent and its child).
That is one logical process, not two servers — don't chase it.

## How collection works

Portal → **Runs** → *Start run*, or on a schedule. One run:

1. **Signs in.** Checks the saved session; if dead, tries an unattended login with
   credentials from `.env`, and only opens a visible window when the site answers with a
   CAPTCHA or 2FA. 1xBet challenges nearly every sign-in, so expect the window.
2. **Walks 5 probes inside one loaded panel** — the panel takes 10–13s to start and every
   probe needs the same one, so it is opened once. Between probes the modal is closed and
   *proven* closed (`reset:` in the yaml); anything less reloads.
3. **Saves** accounts, name-only payees, and evidence (HTML + screenshot, SHA-256).

Takes ~80s. Live progress streams from the subprocess (`ght/progress.py`, marker-prefixed
JSON lines) and renders as a 3-step checklist.

Per-site config is `sources/1xbet-bd.yaml`. Site markup drifts often; a stale selector is a
config fix, not a code fix. The `notes:` block at the bottom holds the recon history.

## Things that will confuse you if you don't know them

- **`ok` vs `partial`.** A run is degraded only by *our* breakage (a selector that no longer
  matches). A method 1xBet has switched off — it answers the click with its own
  "unavailable" panel — leaves the run `ok` and is named in the run's note. Blame and
  completeness are separate: an incomplete run still never concludes an account is *gone*.
- **1xBet switches methods on and off constantly.** Two or three of the five are usually
  off. That is the site, not the collector.
- **A payee is a payee.** Some methods hand off to the provider's checkout, which names a
  business and publishes no number. Those are counted, charted, listed, exported and given
  a detail page exactly like numbered accounts — the screenshot of the page that named them
  is the whole of their evidence. Anything that counts payees counts both.
- **`candidates_found`** on a run means *payees brought back* (de-duplicated accounts +
  distinct names). Rows written before 2026-08-22 hold raw extraction hits and exclude
  name-only payees.
- **One collection at a time, machine-wide.** The collector holds `data/run.lock` (pid +
  slug), so a scheduled run and a hand-started `ght run` cannot race on the login session.

## The portal

Sidebar: Overview, Payees, Runs. Theme toggle in the header. `/components` documents the
recurring UI elements and the reasoning (not linked in the nav).

- **Overview** — accounts found, collections run, sites tracked; accounts by channel;
  newest payees. All read the same query the Payees page reads, so they cannot drift apart.
- **Payees** — one list of both kinds. Search + channel filter, per-page, CSV and PDF
  export. No status column, no confidence: the row says which site it came from.
- **Detail** — one screenshot of the number on the site, plus the sightings.
- **Runs** — manual card and schedule card side by side, then run history. The outcome card
  after a run shows once and stands down on reload.

Dates are Bangladesh format and +06:00 everywhere (`_stamp` / `_day` in `api/routes`).

## Recent work (this is all done and pushed)

Design pass implemented; sign-in automated as far as the site allows; collection 492s → 76s
by sharing one panel; blame separated from completeness with the reason stated on the run;
name-only payees given a page, a screenshot and a place in every count; scheduler added
(`api/schedule.py`, `data/schedule.json`, floor 5 min, first run immediate, skips a slot if
one is still running, survives restart); PDF export (`export/report.py`) with per-page
header, footer, provenance and page numbers; drawn icons throughout.

## Known open items

- **Brand logos are local, not in git.** `data/branding/` is gitignored and ships empty —
  bKash/Nagad/Upay own their marks and an approximation would be a counterfeit. This
  machine has bKash, Nagad, Upay and Bank transfer dropped in, so the portal shows them;
  a fresh clone shows the lettered mark until someone supplies files. Filenames are matched
  loosely (case, spaces and hyphens), so a download goes in as-is. Rocket, Tap and mCash
  have no file yet. The PDF export carries no logos, only the channel label.
- **Bengali payee names in PDFs** need a Unicode font on the machine (Nirmala on Windows).
  Without one the report prints `?` rather than wrong glyphs. No Bengali names in the data
  yet, so this path is untested against real data.
- Portal has **no authentication** and binds to loopback. It must sit behind an
  authenticating proxy before production. Don't bind it to `0.0.0.0`.
- `demo-site` rows (3 failed fixture runs) are still in the database; harmless.

## Working agreements

- Verify against the real thing rather than assuming — run it, screenshot it, check the DB.
  Several bugs this month were only visible in a live run.
- Tests must stay offline and pass before committing.
- A CAPTCHA is never defeated or worked around; when one appears, a person clears it.
  Credentials the operator puts in `.env` may be filled into the site's own login form, and
  live nowhere else — not in `sources/*.yaml`, the database, a log line or a run report.
- Nothing sensitive in git: `data/` (sessions, DB, evidence, schedule) and `.env` are
  ignored, and real collected account numbers must not go into code, tests, or comments.
- Commit and push after each working change, with a message explaining *why*.
- A run against 1xBet initiates one real (unpaid) deposit request via the `fast-nagad`
  probe. Ask before starting one.
