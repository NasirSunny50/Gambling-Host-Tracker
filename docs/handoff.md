# Handoff — paste this to start a new chat

I'm continuing work on **Gambling Host Tracker** at
`D:\Projects\Personal\Gambling Host Tracker` (git repo, pushed to
`github.com/NasirSunny50/Gambling-Host-Tracker`, branch `main`, all work committed).

Read `docs/architecture.md` first — it is the current architecture and technology
document, and `README.md` after it if you need the operational detail. Read code only as
you need it; don't survey the whole tree.

## What it does

Collects the mobile-wallet and bank account numbers gambling sites publish for deposits, so
a bank's AML team can blocklist them. Numbers rotate daily, so it keeps every sighting and
stores the page it came from, hashed, as evidence. Two targets, `1xbet-bd` (5 probes) and
`melbet-bd` (14 probes), running the same platform — same login form, same embedded panel,
same modal markup — under different method ids.

About 94 accounts and 26 distinct merchant names collected so far, over 132 fetches.

## How to run things

```
.venv\Scripts\python.exe -m pytest -q         # 267 tests, all offline, ~15s
.venv\Scripts\python.exe -m ruff check src tests docs
scripts\Start.bat                             # portal on http://127.0.0.1:8000
.venv\Scripts\python.exe docs\build_pdf.py    # regenerate the architecture PDF
```

Always use `.venv\Scripts\python.exe`, never the system Python.

**A running portal holds its Python modules in memory.** Editing a file changes nothing for
a process that is already up — this cost a round trip once, with a fix that was already
committed. Restart the portal after touching anything under `src/`, and kill the old one
first or the port stays bound:

```
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'ght|serve' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

Note: every `ght.cli` command shows as **two** python processes (a parent and its child).
That is one logical process, not two servers — don't chase it. And a Playwright run that is
killed mid-flight leaves headless browsers behind; they hold sockets and make the next fetch
look like a network failure. Clear them by matching `Temp\playwright` in the command line.

## How a fetch works

Portal → **Fetches** → *Fetch accounts now*, or on a schedule. One fetch:

1. **Signs in.** Checks the saved session by loading the page the fetch actually needs — not
   the shell around it, which a dead session is still served. If dead, tries an unattended
   login with credentials from `.env`, and only opens a visible window when the site answers
   with a CAPTCHA or 2FA. Both sites challenge nearly every sign-in, so expect the window.
2. **Walks every probe inside one loaded panel** — the panel takes 10–13s to start and every
   probe needs the same one, so it is opened once. Between probes the modal is closed and
   *proven* closed (`reset:` in the yaml); anything less reloads.
3. **Runs any discoveries** on that same panel (`discover:` in the yaml) — families of
   methods found at run time rather than named: every E-wallets cell whose name carries a
   channel word (so new bKash/Nagad/Upay modules are collected without a config edit), and
   the Bank Transfer method (walked as a cell — 1xBet dropped its recipient-bank dropdown).
   The `options` kind, which iterates a dropdown's options, exists for the case a site still
   has one. A discovery only opens a modal, never confirms, so it can never start an order.
   See `docs/architecture.md` §6.2. The 1xBet `bank-transfer` and `lightspeed-bkash`
   selectors are modelled, not yet seen live — the yaml notes say what to confirm.
4. **Saves** accounts, name-only payees, and evidence (HTML + screenshot, SHA-256).

1xBet takes ~80s. Melbet takes seven or eight minutes, almost all of it waiting out refused
connections (see below). Live progress streams from the subprocess (`ght/progress.py`,
marker-prefixed JSON lines) and renders as a 3-step checklist.

The site dropdown offers **All sites**, which walks the active ones one after another — never
at once, by handing the CLI no `--site`. With several sites the checklist restarts its three
phases per site, so each is announced as "Site 1 of 2: <name>".

Per-site config is `sources/<slug>.yaml`. Site markup drifts often; a stale selector is a
config fix, not a code fix. The `notes:` block at the bottom holds the recon history.

`ght recon --site <slug>` is how a site gets configured or repaired: it signs in the way a
fetch does and prints every method with its id, every dropdown with its exact option labels,
and — with `--open <method-id>` — the elements in one method's panel. It opens a method and
never clicks further, because the click after that one is the confirm.

## Things that will confuse you if you don't know them

- **`ok` vs `partial`.** A fetch is degraded only by *our* breakage (a selector that no
  longer matches). A method the site has switched off — it answers the click with its own
  "unavailable" panel — leaves the fetch `ok` and is named in its note. Blame and
  completeness are separate: an incomplete fetch still never concludes an account is *gone*.
- **"Every method broke at once" means the session, not the config.** Both brands answer a
  dead session by serving the signed-in shell, embedding the panel, listing every method —
  and covering the lot with their own "The session has expired" dialog, outside the frame
  the probes read. The run now spots that dialog and goes back to sign in; before it did,
  it walked every probe into it and reported "the config may be stale", which sends you to
  fix selectors that are fine.
- **These sites switch methods on and off constantly.** Two or three are usually off. That
  is the site, not the collector. A method can also disappear from the list entirely
  mid-session, as Melbet's Cellfin did.
- **A payee is a payee.** Some methods hand off to the provider's checkout, which names a
  business and publishes no number. Those are counted, charted, listed, exported and given a
  detail page exactly like numbered accounts. Anything that counts payees counts both.
- **`candidates_found`** on a fetch means *payees brought back* (de-duplicated accounts +
  distinct names). Rows written before 2026-08-22 hold raw extraction hits and exclude
  name-only payees.
- **`started_at` before 2026-08-23** was stamped when the row was written, which is after
  collection returns — so older fetches all look instantaneous. Newer ones carry the true
  start.
- **One fetch at a time, machine-wide.** The collector holds `data/run.lock` (pid + slug),
  so a scheduled fetch and a hand-started `ght run` cannot race on the login session.
- **Rocket publishes twelve digits** — the wallet's mobile plus a check digit. The MSISDN
  pattern refuses that by design, so it is read as a wallet only when the block says Rocket,
  and **all twelve are kept** (`018046326747` → `+88018046326747`) — the number the site
  prints is what gets blocklisted. The twelfth digit is derived from the first eleven, so it
  cannot cause a collision; the operator is read off the mobile. Older rows keyed on eleven
  were migrated up where a twelve-digit sighting existed.

## The portal

**What it is called.** The portal never says "collection run": one pass over a site is an
**account fetch**, the sidebar item is **Fetches**, the button is **Fetch accounts now**, and
the history is the **fetch history**. The code still calls the machinery the collection
pipeline and the table is still `collection_runs` — the rename is the vocabulary a reader
sees, not an identifier sweep. Keep new user-facing wording on "fetch".

Sidebar: Overview, Payees, Fetches. Theme toggle in the header. `/components` documents the
recurring UI elements and the reasoning (not linked in the nav).

- **Overview** — payees found, fetches run, sites tracked; payees by channel; newest payees.
  All read the same query the Payees page reads, so they cannot drift apart.
- **Payees** — one list of both kinds. Search + channel filter, per-page, CSV and PDF export.
  Copy icons sit on the number and on the holder name.
- **Payee detail** — the identity, the sightings, and one screenshot **of the panel that
  published that number**. Which screenshot that is has to be established, not assumed: see
  `_screenshot_for` in `api/routes`. Where no stored page can be shown to carry the number,
  no picture is shown — another method's screenshot is evidence of the wrong thing.
- **Fetches** — manual card and schedule card side by side, then paged fetch history. The
  outcome card shows once and stands down on reload. A row opens `/runs/<id>`: when it went,
  how long it took, and what it brought back, with the payees **never seen before marked on
  their own rows**. Evidence is still captured and hashed but is not surfaced here.

Dates are Bangladesh format and +06:00 everywhere (`_stamp` / `_day` in `api/routes`).

## The schedule

`api/schedule.py`, state in `data/schedule.json`. One interval for one target (a slug, or
`all`), a thread that wakes and asks the same run manager the button asks. Floor 5 minutes,
ceiling 24 hours, skips a slot if a fetch is still running and records why, survives a
restart.

**It never fetches on the spot.** Not when the schedule is set, and not when the portal
comes back up with an overdue slot — both used to, and both were surprises that cost a
deposit request. The next fetch is always one interval away.

## Melbet notes

Thirteen numbered payees and one name, from fourteen probes: CellFin Free, Nagad, Rocket,
uPay, Nagad Free (which prints the business name beside the number), Trust Axiata Pay,
Rocket Free, iPay, Nexus Pay, and Bank Transfer once per bank in its dropdown (UCB, Pubali,
Dutch-Bangla, Islami). `nagad-paykassma` is the fourteenth and the only one that costs a
deposit request; it is three documents deep — panel, then paykassma's iframe, then Nagad's
own checkout — which is why a flow step can name the frame it acts in.

**The network drops this host in bursts.** Connections are refused at the TLS layer for
minutes at a time and then work again — local filtering, not the site. The browser fetcher
retries a refused load `MAX_RETRIES` times, 8s apart; this machine's `.env` is set to 6.

## Known open items

- **Brand logos are local, not in git.** `data/branding/` is gitignored and ships empty —
  the providers own their marks and an approximation would be a counterfeit. This machine
  has bKash, Nagad, Upay and Bank transfer dropped in. Filenames are matched loosely (case,
  spaces, hyphens), so a download goes in as-is. Rocket, Tap, mCash, CellFin and iPay have
  no file yet. The PDF export carries no logos, only the channel label.
- **Bengali payee names in PDFs** need a Unicode font on the machine (Nirmala on Windows).
  Without one the report prints `?` rather than wrong glyphs. Untested against real data.
- **Alert delivery is not built.** Alerts are detected and stored; no sender exists.
- Portal has **no authentication** and binds to loopback. It must sit behind an
  authenticating proxy before production. Don't bind it to `0.0.0.0`.
- Older screenshots are full-page captures; `shot:` in the site config now frames the panel
  instead. Existing evidence is left exactly as it was taken.
- `demo-site` rows (3 failed fixture runs) are still in the database; harmless.

## Working agreements

- Verify against the real thing rather than assuming — run it, screenshot it, check the DB.
  Several bugs this month were only visible in a live run, and one wrong-screenshot bug was
  only visible by comparing what the page showed against what the database stored.
- Tests must stay offline and pass before committing.
- A CAPTCHA is never defeated or worked around; when one appears, a person clears it.
  Credentials the operator puts in `.env` may be filled into the site's own login form, and
  live nowhere else — not in `sources/*.yaml`, the database, a log line or a run report.
- Nothing sensitive in git: `data/` (sessions, DB, evidence, schedule) and `.env` are
  ignored, and real collected account numbers must not go into code, tests, or comments.
- Commit and push after each working change, with a message explaining *why*.
- Some probes reach their payee by confirming a deposit, which initiates a real (unpaid)
  deposit request on the operator: `fast-nagad` on 1xBet, `nagad-paykassma` on Melbet, and
  `lightspeed-bkash` on 1xBet (opt-in, see below). All are marked `creates_order`. **Ask
  before starting a fetch that includes one** — and note that "All sites" includes them, and
  a schedule fires them unattended every interval (a 15-minute "all" schedule is ~3 orders
  per cycle — flag that cost to the operator).
- **`lightspeed-bkash` (1xBet) is opt-in and unverified.** It confirms into a new tab that
  wants the payer's own bKash number and, on Next, reveals the receiving account (a real
  number, unlike Fast Nagad which yields only a rotating merchant name). It runs only when
  `GHT_1XBET_PAYER_MSISDN` (an 11-digit number) is set in `.env` — until then the probe is
  skipped and raises no order (`requires_env`). Its new-tab selectors are a best guess: the
  method was off site-side when it was built, so confirm them from the first live capture.
- **Flow steps can now**: `opens_tab: true` on a click follows the payee into a new browser
  tab (`fetchers/browser.py` `_click_into_new_tab`); a `fill` value written as `${NAME}` is
  read from the environment, keeping a phone number out of git.
