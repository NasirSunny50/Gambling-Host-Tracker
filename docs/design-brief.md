# Design brief — Host Tracker portal

Design the full interface for an internal web tool. Everything below is content,
structure, and behaviour. **All visual decisions are yours** — nothing here prescribes a
look, and where I describe an element I mean its job, not its appearance.

---

## 1. What the product is

Gambling sites publish mobile-wallet and bank account numbers for players to deposit into.
Those numbers rotate two or three times a day. This tool visits the sites' deposit pages on
demand, records every payee it finds, and keeps the page it came from as evidence.

The output is a timeline: which account was advertised, on which site, at what time, with
proof. An anti-money-laundering team uses it to build a case and to feed a blocklist.

It is deployed inside a bank.

## 2. Who uses it

**The AML analyst** — the main user. Reads and searches collected accounts, checks where a
number came from, exports for a case file. Not technical. Cares whether a number is current,
who it belongs to, and whether the evidence would survive scrutiny.

**The operator** — often the same person. Presses a button to collect, and must notice when
the tool needs them to do something (see §4.5).

Neither is browsing for pleasure. They are usually checking one specific thing, under time
pressure, and will be looking at this alongside other systems.

## 3. Qualities it needs

- **Credible.** The output can end up in a regulatory file. It should look like a system of
  record, not a dashboard demo.
- **Dense but scannable.** Analysts compare many rows at once. Space is not the enemy;
  ambiguity is.
- **Honest about certainty.** Some findings are exact, some are inferred. That difference
  must be visible without being alarming.
- **Calm.** It reports problems constantly (sites break, sessions expire). Routine problems
  should not read as emergencies, or the real ones stop registering.

## 4. Screens

Six screens, one persistent navigation. Every screen is server-rendered; a page reload is
the normal way things update.

### 4.1 Overview

The landing screen. Answers "what do we have, and is collection healthy?"

- Four summary figures: **active accounts**, **seen all-time**, **needs review**,
  **sites tracked**. Each links to a filtered view.
- **Active accounts by channel** — a payment channel and a count, a handful of rows.
- **Newest accounts** — recent finds: channel, account number, holder name, bank, when first
  seen.
- **Recent runs** — when, site, outcome, how many candidates found, how many new, a note.
- **Recent alerts** — when, type, site, detail.

### 4.2 Accounts

The working screen. A filterable table over everything collected.

- Filters: free-text search (matches number, holder, bank), channel, status
  (any / active / gone / needs review). An export to CSV that respects the current filter.
- Columns: channel · account number · holder name · bank · confidence · times seen · last
  seen · status.
- Account numbers are identifiers people copy into other systems. They need to be readable
  digit-by-digit and easy to copy.
- Row count is typically tens to low hundreds.

### 4.3 Account detail

Everything known about one account, and the proof.

- Header: the account number itself, its channel, its status.
- Attributes: holder, bank, branch, operator, account type, confidence, first seen, last
  seen.
- **Seen on** — which sites advertised it, first/last seen per site, how many times. An
  account appearing on several sites is the single most significant finding this tool
  produces; the design should let that land.
- **Observations** — every individual sighting: when, which run, the raw text as it appeared
  on the page, which selector matched it, confidence.
- **Evidence** — stored page captures and screenshots for those runs: run, kind, a SHA-256
  digest, size, path. This is the audit trail; it should feel like one.

### 4.4 Merchants

Some payment routes never show an account number — they name a business instead, and the
name changes on every request. These cannot be de-duplicated into accounts, so they
accumulate as sightings and nothing is ever marked "gone".

- Needs a short explanation of why this list behaves differently from Accounts.
- Columns: merchant name · channel · which probe found it · times seen · first seen · last
  seen.

### 4.5 Runs

Where collection is started and watched. **This is the screen that most needs your
attention.**

**Idle state.** Choose a site, press one clear action to start. A line of explanation: it
signs in first, then reads every payment method, and takes a few minutes. If the chosen site
has a method that initiates a (never-paid) deposit request, the user is warned and must
confirm.

**Running state.** A run has three named phases, in order:

1. Sign in to the site
2. Read each payment method
3. Save accounts and evidence

The screen shows which phase is active, which are done, which are pending, plus a live
message and, during phase 2, a counter such as "Reading upay — 2 of 10". The page refreshes
itself while a run is in flight. A run takes roughly ten minutes, so this state is looked at
repeatedly and should stay informative rather than decorative.

**The waiting-for-you state — the hardest moment in the product.** The sites are protected
by a CAPTCHA that cannot be solved automatically. When the saved session has expired, the
tool opens a browser window *outside this page* and waits up to five minutes for the person
to sign in there. Meanwhile this screen must make three things unmissable:

- the run has not stalled — it is waiting for **them**
- there is a browser window elsewhere on their machine that needs attention
- once they sign in, everything continues on its own; nothing further is required here

If they miss it, the run expires and the work is wasted. This state needs to be
distinguishable at a glance from ordinary progress, and it must not look like an error —
it is a normal step.

**Finished state.** Whether the last run completed or stopped early, with its output
available to read.

**History.** Past runs: id, when, site, outcome, candidates found, new accounts, evidence
count, and an error note where relevant.

### 4.6 Alerts

A log of things worth a human's attention: when, type, site, related account, detail,
whether it was delivered onward.

## 5. Vocabulary and states

Design distinct treatments for these. Several appear as short status labels in tables.

**Account status** — `active` · `gone` · `needs review`

**Confidence** — a value from 0 to 1, shown as a percentage. Roughly: 0.9+ came from a
configured selector and is trustworthy; 0.6–0.9 was inferred from surrounding text; below
that came from a broad text sweep and needs review. Only high-confidence findings go into
an automatic blocklist feed, so the distinction is consequential.

**Run outcome** — `ok` · `partial` (ran, but something on the site had changed) · `failed` ·
`blocked` (bot protection)

**Alert types** — `new_account` · `account_disappeared` · `extractor_broken` · `flow_broken`
· `probe_failed` · `site_down` · `site_blocked` · `auth_expired` · `auth_refreshed`

Some of these are informational (`new_account`, `auth_refreshed`), some mean maintenance is
due (`extractor_broken`, `flow_broken`), some mean collection is down (`site_down`,
`site_blocked`). The design should let a reader sort them into those three groups without
reading every word.

**Payment channels** — bKash · Nagad · Rocket · Upay · Tap · mCash · Bank transfer. These
are distinct Bangladeshi services; users think in terms of them and scan for them
constantly. They deserve to be quickly distinguishable from one another.

## 6. States to design, not just the happy path

- **First run, nothing collected yet** — every screen is empty. An empty screen should say
  what belongs there and what to do to fill it, rather than showing a blank table.
- **Filtered to nothing** — different from "nothing exists"; the way out is different too.
- **A run in progress**, including the waiting-for-sign-in case above.
- **A long-running or stalled run.**
- **Missing values.** Holder, bank, branch and operator are frequently unknown. Roughly half
  the cells in a typical Accounts table are empty. Absence should be legible without
  becoming visual noise.
- **Long values.** Business names run to forty characters; digests and file paths are longer
  still. Tables need to cope without becoming unreadable.

## 7. Constraints

- **Desktop-first.** Analyst workstations, wide screens, alongside other windows. It should
  remain usable narrower, but no phone design is needed.
- **Built as plain server-rendered HTML with one stylesheet.** No JavaScript framework, no
  component library.
- **Fully self-contained and offline.** No external fonts, icon packs, images, or scripts of
  any kind — these machines may have no internet access, and a tool holding evidence should
  not call out to third parties on page load. Anything visual has to be achievable in CSS or
  inline markup.
- **Must work under both light and dark viewing preferences.**
- **WCAG 2.1 AA.** Keyboard navigable, screen-reader sensible. Status and confidence must
  never be conveyed by colour alone.
- **Text in English**, but content includes Bengali business names and occasional Bengali
  digits, so glyph coverage and mixed-script lines matter.

## 8. What to deliver

1. The six screens at a realistic data density — with plausible content, not placeholder
   text. Numbers look like `+8801XXXXXXXXX` (mobile wallets) or 13–17 digits (bank
   accounts); holders are small-business names.
2. The Runs screen in each of its states: idle, running, waiting for sign-in, finished,
   and with no history.
3. At least one meaningful empty state and one filtered-to-nothing state.
4. A component inventory: whatever recurring elements you settle on — status labels,
   channel indicators, confidence, tables, summary figures, section headings, the progress
   display, empty states, forms and actions.
5. Brief notes on the reasoning where a choice is not self-evident, particularly around the
   waiting-for-sign-in moment and how certainty is expressed.

## 9. Deliberately not specified

Colour, typography, spacing scale, shape language, iconography, density, motion, and overall
visual character are all open. Where I have described something as a "label" or "figure",
read that as its function; choose whatever form serves it.

If a structure here works against a better design, say so and propose the alternative — the
screens are a description of the information that exists, not a layout.
