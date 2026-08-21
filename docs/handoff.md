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

Just finished: the design pass from `design/Portal Pages Design Scope.zip` is implemented
across every template — sidebar shell with a light/dark toggle, channel marks, status and
outcome pills, confidence as percentage + bar + provenance word, the distinction between
"not applicable" and "unknown", and the run states (idle, running, waiting for sign-in,
finished). New page: **Components & notes** at `/components`, which shows the recurring
elements and the reasoning behind them. `tests/test_portal_templates.py` renders every page
offline, including the run states that otherwise need a live collection to reach.

`docs/design-brief.md` is the brief that pass was made against.

## Known open items

- Collection could be ~2× faster by reusing one browser across probes instead of launching
  per probe. Not done — was judged not worth the risk yet.
- Portal has **no authentication** and binds to loopback. It must sit behind an
  authenticating proxy before production. Don't bind it to `0.0.0.0`.

## Working agreements

- Verify against the real thing rather than assuming — run it, screenshot it, check the DB.
- Tests must stay offline and pass before committing.
- Never enter credentials or defeat a CAPTCHA; the operator signs in themselves.
- Nothing sensitive in git: `data/` (sessions, DB, evidence) and `.env` are ignored, and
  real collected account numbers must not go into code, tests, or config comments.
- Commit and push after each working change, with a message explaining *why*.
