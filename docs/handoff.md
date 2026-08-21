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
.venv\Scripts\python.exe -m pytest -q         # 143 tests, all offline
.venv\Scripts\python.exe -m ruff check src tests
scripts\Start.bat                             # portal on http://127.0.0.1:8000
```

Always use `.venv\Scripts\python.exe`, never the system Python. Kill stale servers with
PowerShell before restarting, or the port stays bound and you'll test old code:
`Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'serve|uvicorn|ght' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }`

## How collection works

Portal → **Runs** → *Run collection*. The run:

1. **Signs in first.** Checks the saved session headlessly; if it's dead, opens a *visible*
   browser window and waits up to 5 min for the operator to sign in (1xBet has a CAPTCHA
   that is never solved automatically — that's deliberate policy, don't try).
2. **Walks 8 probes**, one per payment method, inside an iframe on the deposit page.
3. **Saves** accounts, merchant names, and evidence.

Live progress streams from the subprocess to the portal (`ght/progress.py`, marker-prefixed
JSON lines) and renders as a 3-step checklist. Takes ~1.5 min. Last run: `ok`.

Per-site config is `sources/1xbet-bd.yaml` — selectors, probes, login block. Site markup
drifts often; a stale selector is a config fix, not a code fix.

## Recent state

The portal design pass from `design/Portal Pages Design Scope.zip` is implemented across
every template - sidebar shell with a light/dark toggle, channel marks, status and outcome
pills, confidence as percentage + bar + provenance word, the "not applicable" vs "unknown"
distinction, and the run states (idle, running, waiting for sign-in, finished). New page:
**Components & notes** at `/components`.

Then two faults behind one screenshot - a failed run shown with three green ticks and a
"Run finished" heading:

- The checklist was keyed on the subprocess exit code, and `ght run` exits 0 even when it
  records a failed collection. Failure now travels with the phase: progress updates carry
  an `ok` flag, the pipeline names the phase it stopped at, and the manager keeps that.
- The session check judged the account page while collection needs the embedded payment
  app, which is what an expired session is actually refused. Verified live: the page loads
  clean and the `/paysystems/deposit` frame never appears. The check now waits for that
  frame (`require_frame=True`, and only where the page being judged *is* the deposit page -
  the assisted window must not demand it, or a good sign-in on the homepage is rejected).

Sign-in is now automated as far as the site allows: `ght.credentials` reads
`GHT_LOGIN_<SLUG>_USERNAME` / `_PASSWORD` from the environment or `.env`, and
`perform_login` tries unattended first, falling back to the visible window only when the
site answers with a CAPTCHA or 2FA. Those are never worked around. 1xBet challenges nearly
every sign-in, so there the credentials save the typing rather than the person. Sessions
also roll forward now - a signed-in fetch writes the refreshed cookies back, guarded so a
logged-out capture can never overwrite a good session.

`docs/design-brief.md` is the brief the design pass was made against.

## Known open items

- Nobody has run a collection since the sign-in changes. The stored session is known dead,
  so the next run will open the window - that is the path to watch.
- Collection could be ~2× faster by reusing one browser across probes instead of launching
  per probe. Not done — was judged not worth the risk yet.
- Portal has **no authentication** and binds to loopback. It must sit behind an
  authenticating proxy before production. Don't bind it to `0.0.0.0`.

## Working agreements

- Verify against the real thing rather than assuming — run it, screenshot it, check the DB.
- Tests must stay offline and pass before committing.
- A CAPTCHA is never defeated or worked around; when one appears, a person clears it.
  Credentials the operator puts in `.env` may be filled into the site's own login form, and
  live nowhere else — not in `sources/*.yaml`, the database, a log line or a run report.
- Nothing sensitive in git: `data/` (sessions, DB, evidence) and `.env` are ignored, and
  real collected account numbers must not go into code, tests, or config comments.
- Commit and push after each working change, with a message explaining *why*.
