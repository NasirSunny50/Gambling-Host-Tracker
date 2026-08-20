# Gambling Host Tracker

Collects the mobile-banking and bank account numbers that gambling sites publish for
deposits, so an AML team can blocklist them and build a case file.

Those numbers rotate two or three times a day, so the collector runs on a schedule and
keeps **every sighting** rather than the current value. What you get back is a timeline:
which account was advertised, on which site, at which time, with the page it came from
stored and hashed as evidence.

> **Scope.** The system never creates an account and never completes a payment. Some
> deposit pages sit behind a login, so it can reuse a session a human captured by hand (no
> credentials are stored), and reaching a few payees requires confirming a deposit, which
> *initiates* a request the operator shows but which is never paid. Collected data includes
> account holder names and numbers — treat it under the organisation's existing PII and AML
> retention policy.

## Quick start (Windows)

Double-click, in order:

1. **`Before Start.bat`** — opens a browser so you sign in yourself, then saves the session.
   Run it again whenever a collection starts coming back empty (the session has expired).
2. **`Start.bat`** — launches the portal at `http://127.0.0.1:8000` and opens it in your
   browser. Trigger collections from the **Runs** page; keep the window open while you work.

The first run of either sets up the environment automatically. Everything below is the
manual equivalent for other platforms.

## Setup

```bash
python -m venv .venv && ./.venv/Scripts/python.exe -m pip install -e ".[dev,api,export,sched]"
```

```bash
cp .env.example .env && ./.venv/Scripts/python.exe -m alembic upgrade head
```

Local development uses SQLite. Point `DATABASE_URL` at PostgreSQL for production — no code
changes are needed, the models avoid Postgres-only column types on purpose.

For JavaScript-rendered sites, add the browser fetcher:

```bash
./.venv/Scripts/python.exe -m pip install -e ".[browser]" && ./.venv/Scripts/playwright.exe install chromium
```

## Adding a target site

1. **Recon first.** Open the deposit page and note where each number sits, what labels it,
   and whether it is rendered server-side or by JavaScript. Save the page into
   `tests/fixtures/html/` — that fixture is what keeps the extractor honest later.
2. Copy `sources/demo-site.yaml` to `sources/<slug>.yaml` and fill in the URLs and blocks.
   Each block pairs a channel with the element holding its number:

   ```yaml
   blocks:
     - channel: bkash
       container: ".payment-method.bkash"
       value: ".account-number"
   ```

   Because the channel is stated in config rather than guessed from nearby words, these
   hits are recorded at high confidence and flow straight into the export.
3. Add any published-but-irrelevant numbers (support hotlines, WhatsApp contacts) to
   `ignore_numbers`.
4. Check it with `ght run --site <slug> --dry-run`, which prints what it found and writes
   nothing.

These YAML files live in git deliberately: when someone rewrites a selector after a site
redesign, the git history is the record of what the collector was looking for on any date.

## Commands

```bash
ght sites                              # configured targets
ght run --site demo-site --dry-run     # extract and print, write nothing
ght run                                # collect from every active site
ght accounts --all --channel bkash     # what has been collected
ght status                             # recent runs and their outcomes
ght export --out accounts.csv          # CSV for the AML team (logged in access_log)
ght verify-evidence                    # re-hash stored blobs against their recorded digest
ght serve                              # read-only web portal over the collected data
```

## Login-gated sites

Some deposit pages sit behind a login and only reveal the payee after a multi-step flow
(pick a method, confirm, follow a redirect or open a modal). Those are configured with a
`browser` fetcher and per-method `probes`, and the session is captured by hand so no
credentials ever live in the repo:

```bash
python scripts/collect_1xbet.py --login   # opens a real browser; you sign in yourself
python scripts/collect_1xbet.py           # then collects unattended, reusing the session
```

The saved session (`data/auth/…`) and everything under `data/` are gitignored. A probe
marked `creates_order` reaches its payee by confirming a deposit, which initiates a deposit
request on the operator — no funds move — and the portal names those probes before you run.

## Portal

`ght serve` starts a server-rendered portal (dashboard, searchable accounts, per-account
evidence trail, runs, alerts, merchant sightings) with a CSV export. The Runs page can
launch a collection in the background, one at a time, and every search, export, run and
login is written to `access_log`.

## Login & session recovery

Deposit pages sit behind a login, and the sites use bot protection (a CAPTCHA on sign-in)
that can't be solved automatically. So login is **assisted**, not headless:

- A site with a `login:` block marked `assisted: true` (form selectors + a success marker)
  supports one-click sign-in.
- When **Run collection** finds the session expired, a **visible browser window opens** for
  the operator to sign in — solving the CAPTCHA and pressing the site's login button
  themselves. The moment the success marker appears, the session is captured and collection
  continues in the same run (an `auth_refreshed` alert records the self-heal).
- No credentials are stored anywhere: the person types them into the window. If nobody
  completes the sign-in within a few minutes, the run reports the expiry and stops.

> **This needs a desktop.** The assisted window has to appear on a screen the operator is
> looking at, so run the portal on that machine (the Windows launchers do this). A headless
> server can't show the window — there, refresh the session out-of-band with
> `scripts/collect_1xbet.py --login` and let unattended runs reuse it.

> **Before production.** The portal has no authentication and binds to loopback
> (`127.0.0.1`) on purpose. It must sit behind an authenticating reverse proxy before it is
> exposed beyond localhost — do not bind it to `0.0.0.0` without that in place.

## How it holds up when a site changes

Every run extracts twice: once through the configured selectors (precise, channel known)
and once with a full-page regex sweep (noisy, channel guessed by proximity). The gap
between them is the health signal.

- Selectors find numbers → normal run, high confidence.
- Selectors find nothing **but the sweep still does** → the site was redesigned and the
  config is stale. The run is marked `partial` and an `extractor_broken` alert is raised,
  instead of quietly reporting zero numbers.
- Nothing loads at all → `site_down`; a challenge page or 403 → `site_blocked`, tracked
  separately because it needs a different fix.

Raw pages are stored before extraction, so once a selector is repaired the old captures can
be re-processed rather than lost.

## Data model

| Table | What it holds |
|---|---|
| `observations` | Append-only. One row per sighting per run. Never updated. |
| `accounts` | The de-duplicated account behind those sightings, with first/last seen. |
| `account_sites` | Which sites advertised an account — the same account on several brands is the strongest signal here. |
| `evidence` | Stored page bytes, addressed and verified by SHA-256. |
| `collection_runs` | Every fetch attempt and its outcome. |
| `access_log` | Who searched or exported the data. |

An account stays `is_active` for 48 hours after its last sighting: a number that rotated
out this morning was still collecting deposits today and belongs on the blocklist.

## Tests

```bash
./.venv/Scripts/python.exe -m pytest
```

The suite runs entirely offline. The pipeline tests serve the saved fixtures over a local
HTTP server, so the fetcher, evidence store, de-duplication and changeset logic all run the
same way they do in production.

## Status

In place: schema, collection pipeline, both extractors, evidence store, login-gated
multi-step browser collection with per-method probes, CLI, CSV export, and the read-only
portal with background run control. Still to come — the scheduler (3×/day, jittered) and the
Teams/Email/Telegram alert senders.
