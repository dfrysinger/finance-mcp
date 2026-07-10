# Web UI — Month-based date navigation, Spending subtabs, tab memory

Status: **design locked, built** · Last updated: 2026-07-10

This document is the single source of truth for a set of usability changes to
the local review web UI (`src/finance_mcp/webui.py`, served by
`finance-mcp web`). It contains no personal data: the UI renders whatever the
user's own archive and budget config hold.

## Problem

The web UI's date-driven tabs (Transactions, Spending, Allocation,
Subscriptions, Burn-down) previously exposed raw `start`/`end` date inputs with
no default, so each tab loaded empty until the user hand-typed a range. Two
further friction points:

1. **Spending** grouped its results through a `group_by` dropdown, which hides
   the available groupings behind a click and reads as a filter rather than a
   primary view axis.
2. The UI always opened on the first tab (Accounts); the user's actual working
   tab was forgotten on every reload.

## Goals

1. Every **month-scoped** tab (Transactions, Spending, Burn-down) defaults to the
   **current calendar month** and offers **‹ / ›** arrows to step one month at a
   time, reloading on each step.
2. A **custom** (arbitrary start→end) range is available behind a calendar icon,
   so the common case (this month) is one glance and the rare case (custom
   range) is one click away — not the default clutter.
3. **Spending**'s `group_by` becomes a row of **subtabs**
   (`category / account / envelope / org / month`) instead of a dropdown, and
   the chosen grouping is remembered.
4. The UI **remembers the last tab** the user was on across reloads.
5. **Audit-window** tabs (Subscriptions, Allocation) load populated by default
   without being forced into a single month.

Non-goals: changing any backend endpoint's parameters or semantics beyond the
date-inclusivity fix below; altering the forward-looking Forecast tab or the
single-date Red flags tab (neither is a month range).

## Scope decision: month-scoped vs audit-window tabs

Not every date-range tab is month-scoped. Subscriptions and Allocation are
**multi-month audit windows**: the subscription detector needs several months of
history to recognize a recurring cadence (`min_occurrences` defaults to 3), and
a single-month window both hides untracked candidates and falsely reports
tracked bills as "missing" (their prior occurrences fall outside the window).
Forcing them to the current month would break them.

Therefore the month navigator applies only to the genuinely month-scoped tabs
(Transactions, Spending, Burn-down). Subscriptions and Allocation keep an
explicit start→end date picker, but gain a **trailing-window default** (first day
of the month five months ago → today, i.e. ~6 months) so they load populated
instead of empty — solving the same "loads blank until I type a range" pain
without the month straitjacket.

## Backend date-inclusivity fix (tightly coupled)

Making the current month the default surfaced a latent backend bug:
`queries.filter_transactions` parses a bare `end_date` (`YYYY-MM-DD`) to UTC
**midnight**, while archived rows are stored at `T12:00:00+00:00` (noon), so the
`ts > end_ts` guard silently drops **every transaction on the last day of the
range**. Before this change the empty default hid it; the current-month default
makes it bite every month. The fix makes a bare date `end_date` **inclusive
through the end of that day** (Transactions and Spending both route through
`filter_transactions`, so both are fixed). Time-precise callers (full ISO
timestamps) are unaffected.

## Approach

All changes are confined to the embedded `INDEX_HTML` string in
`src/finance_mcp/webui.py`; no Python endpoint changes. The existing generic
tab/filter machinery (`TABS` config → `buildFilters()` → `collectParams()` →
`load()`) is extended, not replaced:

- **Tab config gains three optional descriptors.** A tab may declare
  `range:{start,end}` (the two backend date keys it drives; Transactions,
  Spending), `month:"<key>"` (a single-month key; Burn-down), and/or
  `subtabs:{k,opts}` (a primary-axis selector; Spending). The raw
  `start`/`end`/`group_by` entries are removed from those tabs' `filters` arrays
  so they are owned by the navigator/subtabs, not rendered as manual inputs.
  Subscriptions and Allocation keep their `start`/`end` **date filters** but with
  computed trailing-window defaults.
- **`renderMonthNav()`** renders the ‹ label › + calendar-icon control, holds
  per-tab session state (`{mode, y, m, custom}`), and writes its resolved values
  into hidden `<input id="f_<key>">` elements so `collectParams()` picks them up
  unchanged. A cleared custom month input resolves to an empty param, never
  `"-01"`.
- **`renderSubtabs()`** renders the grouping buttons and writes the active value
  into the same hidden-input channel.
- **`collectParams()`** is widened to read the `range`/`month`/`subtabs` keys in
  addition to the manual `filters`.
- **`load()`** stamps each request with a monotonic sequence number and discards
  a response if a newer request has started, so rapid tab/month/subtab clicks
  can't let a stale fetch overwrite fresher content.
- **Persistence** uses `localStorage` (guarded by try/catch for private-mode):
  `fmcp.lastTab` (restored in `init()`) and `fmcp.sub.<tabId>` (the per-tab
  subtab selection).

Month state is **session-only**: navigating months persists while the page is
open (so tab-switching keeps your place), but a full reload returns every tab to
the current month — which is the intended default per Goal 1.

## Invariants + acceptance criteria

Enforcement truth lives in the invariant register
(`docs/architecture/INVARIANTS.md`); this doc references the IDs.

- **INV-WEBUI-001** — Month-scoped tabs Transactions and Spending declare
  `range:` and do **not** list their `start`/`end` date keys as manual
  `filters`; Burn-down declares `month:`. (Subscriptions and Allocation are
  audit-window tabs and intentionally keep explicit `start`/`end` date filters.)
- **INV-WEBUI-002** — Spending declares `subtabs:{k:"group_by", ...}` and does
  **not** expose `group_by` as a `select` filter.
- **INV-WEBUI-003** — Tab selection is persisted to and restored from
  `localStorage` under `fmcp.lastTab`.
- **INV-WEBUI-004** — The embedded UI script is syntactically valid JavaScript.
- **INV-WEBUI-005** (behavioral) — The date helpers compute correct bounds:
  a month resolves to its true first/last day, and stepping crosses year
  boundaries correctly (Jan ‹ → Dec of prior year; Dec › → Jan of next year).
- **INV-QUERIES-001** (behavioral) — `filter_transactions` treats a bare
  `YYYY-MM-DD` `end_date` as inclusive through the end of that day, so a
  transaction stored at noon on the last day of the range is returned.

Acceptance (observable): loading the UI shows the current month on Transactions,
Spending, and Burn-down; ‹ / › change the month and reload; the calendar icon
toggles a custom start→end picker; Spending shows a subtab row and remembers the
choice; Subscriptions and Allocation load populated over a trailing multi-month
window; reloading returns to the last-viewed tab; a last-day-of-month
transaction appears in the month's Transactions and Spending.

## Check definitions

| Invariant | Check | Where | CI |
|---|---|---|---|
| INV-WEBUI-001 | Assert `TABS` text: Transactions/Spending have `range:` and no `start`/`end` manual filter; Burn-down has `month:` | `tests/test_webui_design_guards.py` | pytest (always) |
| INV-WEBUI-002 | Assert Spending has `subtabs:{k:"group_by"` and no `group_by` `type:"select"` filter | same | pytest (always) |
| INV-WEBUI-003 | Assert `INDEX_HTML` writes and reads `fmcp.lastTab` via `localStorage` | same | pytest (always) |
| INV-WEBUI-004 | Extract `<script>` and `node --check` it | same (skips if `node` absent) | pytest (Node present on CI runners) |
| INV-WEBUI-005 | Extract the pure date helpers, run them under Node, assert bounds + year-wrap | same (skips if `node` absent) | pytest (Node present on CI runners) |
| INV-QUERIES-001 | `filter_transactions` returns a noon-stamped txn on the last day when `end_date` is that bare date | `tests/test_queries.py` | pytest (always) |

These are all deterministic (no LLM judge), split structural (001–004) vs
behavioral (005, QUERIES-001), and stay wired into the existing `pytest -q` CI
job.
