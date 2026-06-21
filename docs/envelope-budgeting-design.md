# Envelope Budgeting — Design

Status: **design locked, building iteratively** · Last updated: 2026-06-21

This document is the single source of truth for the envelope-budgeting feature
track in finance-mcp. It is intentionally free of any personal data: every
account number, dollar amount, and recurring bill is **user-supplied config**,
never hardcoded. That separation is what lets anyone — not just the original
author — run this system against their own budget.

## Problem

finance-mcp already pulls transactions and reconciles internal transfers
(see [`transfer-reconciliation`](#relationship-to-transfer-reconciliation)).
On top of that, a user who runs an **envelope budget** — one bank account per
spending category, paychecks fanned out to envelopes on a schedule, spending as
debits out of each envelope — wants to know, in priority order:

1. **Burn-down** — am I over- or under-spending each envelope this month?
2. **Sufficiency / forecast** — will each envelope cover its known upcoming bills?
3. **Allocation audit** — did the scheduled paycheck→envelope transfers actually
   fire, on the right day, for the right amount?
4. **Subscription audit** — did an expected recurring charge fail to land (a sign
   of a billing problem or a cancellation), and are there new recurring charges we
   don't yet track?

## Core principles

- **SSOT.** Budget intent lives in exactly one place per concern:
  - Category rules → the `category_rules` table (already exists).
  - Envelope map + monthly targets + recurring calendar → a single user-supplied
    **budget config file** (see [Budget config](#budget-config)). No second copy.
- **Config is data, not code.** The envelope→account mapping and targets are
  parsed from the user's config at runtime. Nothing about one person's budget is
  baked into the source. This is the reusability contract.
- **Scripted vs. LLM split.** Deterministic facts and arithmetic are scripted and
  unit-tested; fuzzy judgment is left to the LLM assistant at query time. Scripts
  emit structured, auditable facts and *candidate lists*; the assistant reasons
  over them. See [the split table](#scripted-vs-llm-boundary).
- **No fake data.** Consistent with the rest of finance-mcp: real data or an
  honest "needs source" / blank state. Test fixtures are synthetic by design and
  live only in the test suite.
- **Robust + tested.** Every piece ships with a unit + end-to-end test suite and
  passes review before it lands. Built for others to depend on.

## Data sources

| Source | Role |
|---|---|
| SimpleFIN live sync | Rolling ~90-day window of fresh transactions (existing path). |
| Exported statements | Multi-year history that predates the SimpleFIN window. Per-envelope bank CSVs plus per-card CSVs in several distinct schemas. Imported once into the archive (see Piece A). |
| Budget config | The authored plan: envelope→account map, monthly target per envelope, and the recurring-charge calendar (expected merchant, amount, cadence, paying envelope). User-supplied. |

The exported-statement importer is **reference-only reimplemented** from any
pre-existing one-off categorization harness: finance-mcp has a real schema,
idempotent upsert keyed on a stable id, and a test suite that a one-off script
lacks, and we refuse to introduce a second source of truth for category rules.

## Pieces

Built and reviewed one at a time, in dependency order.

### Piece A — Statement import (foundation) · **built** (`src/finance_mcp/importer.py`)

A CSV importer that reads each supported export schema and writes archive
`transactions` rows.

- **Format adapters.** One small adapter per source schema (per-envelope bank
  export, and each card export: Schwab, Apple Card, Chase, Fidelity). Each adapter
  maps its columns onto the archive's normalized transaction shape and asserts its
  own sign convention. New formats are added as new adapters, not by editing a
  monolith. Adapters are matched by their header set; a `forbidden` header set
  disambiguates schemas that share a required core (e.g. Apple vs. Chase).
- **Stable synthetic ids.** Imported rows get a deterministic id derived from the
  stable natural key `(source, account, date, amount, description, payee,
  occurrence)` so re-importing the same file is idempotent and an imported row can
  be reconciled/deduped against a SimpleFIN row for the same transaction.
  Optional / version-dependent detail columns (transaction type, memo, check
  number, cardholder) are stored display-only in `memo` and kept OUT of the id, so
  an export that omits one does not re-key. The amount is scale-canonical in the id
  (5.0 / 5.00 / 5 → one id) without rounding; the id payload is JSON-encoded so a
  field containing the delimiter cannot collide two distinct rows.
- **Fail-loud parsing.** Malformed CSV quoting is parsed strictly and surfaced as a
  skipped file rather than silently swallowing following rows; a schema-matched file
  that yields zero rows is reported, not hidden as a clean import.
- **Account identity.** Each adapter derives the account from the filename. When no
  account number can be derived (a renamed / non-standard export), the row still
  imports under a generic per-source account but the file is surfaced in the import
  summary's `warnings` so it is never *silently* misfiled. Apple Card has no
  per-account number and uses a fixed id by design (not flagged).
- **Idempotent upsert.** Re-running import never duplicates and never clobbers a
  hand-assigned category (categories live in their own table).
- **Output.** A richer multi-year `transactions` archive. The existing transfer
  reconciler and product-type map run over it unchanged.

### Piece B — Envelope burn-down (priority 1) · **built** (`src/finance_mcp/burndown.py`)

For a given month, per envelope: **planned target vs. actual spend**, where
actual spend = envelope outflows **minus reconciled inter-envelope transfers**
(transfers are allocation flow, not spend — the reconciler already isolates them).

- **Budget config is the SSOT for intent** (`budget_config.py`). A user-supplied
  JSON file (default `~/.finance-mcp/budget.json`, override with `--config`) maps
  each envelope to a name, one or more account ids, and an optional monthly
  target. Validation fails loud: a blank name, an account claimed by two
  envelopes, or a target that is not a whole number of cents raises rather than
  producing a quietly-wrong budget. Unknown keys are ignored so later pieces can
  extend the same file (the recurring calendar) without breaking older readers.
- **Spend is outflow, not net of refunds.** Headline `actual_spend` is the sum of
  an envelope's non-transfer outflows. Refunds (non-transfer, non-income credits)
  and net-of-refund spend are reported alongside but never folded into the
  headline — netting a stray credit into spend would underreport and hide an
  overrun.
- **Integer cents, never floats.** Every amount is parsed from the authoritative
  decimal string into integer cents, so a `750.0` target never compares `> 750.00`
  and flags a false overrun.
- **Transfers excluded two ways, both counted.** A transaction is not spend if the
  categorizer flagged it `is_transfer` or it is a leg of a reconciled transfer
  link (status confirmed/inferred only — an unmatched single leg stays in spend).
  Until the reconcile piece runs, exclusion comes from the category flag; the
  report surfaces both counts.
- **Nothing silently dropped.** Outflow on an account in no envelope is surfaced
  in an `unmapped` bucket. Envelopes with no transactions still appear at zero.
- **Month by posted date.** Transactions are bucketed by the bank's posted date
  string (`YYYY-MM`), matching the rest of the tool and sidestepping any
  timezone/DST boundary error. An undated row cannot be placed in any month, so —
  like a row from a different month — it is skipped silently; only in-month rows
  whose amount cannot be parsed are surfaced as an `amount_missing` diagnostic.
- **Output.** Per envelope: target, outflow, refunds, actual spend, net spend,
  remaining headroom, over/under flag, percent used. Plus totals and the unmapped
  bucket. Pure function over categorized transactions + config + reconciled legs;
  a thin wrapper loads those from the archive. CLI: `finance-mcp burndown
  --month YYYY-MM`.

### Piece C — Sufficiency / forecast (priority 2) · **built** (`src/finance_mcp/forecast.py`)

For each envelope: current balance + scheduled inflows for the horizon vs. known
upcoming bills from the recurring calendar → "will it cover what's coming, and by
when is it at risk." A deterministic running-balance projection over the calendar.

- **Recurring calendar in the config** (`budget_config.py`, same file). Two new
  sections, both forward-compatible (older readers ignore them):
  - `recurring`: bills. Each binds a `name`, a paying `envelope` (must match an
    existing envelope), a positive `amount` (whole cents), a `cadence`
    (`monthly`), and a `day` (1–31, due day-of-month, clamped to each month's
    length so 31 → Feb 28/29).
  - `scheduled_transfers`: inflows into envelopes. Each has a `to` envelope, an
    optional `from` envelope, an `amount`, a `cadence`, and a `day`. **When
    `from` is set the transfer is internal** (e.g. paycheck hub → category
    envelope) and emits a paired *debit on the source* and *credit on the
    destination*, so a fan-out can never credit one envelope without debiting
    its funder — money is conserved. With no `from` it is an external inflow
    (a direct deposit straight into the envelope), credit only.
  - Validation fails loud: a bill or transfer naming an unknown envelope, a
    non-whole-cent/negative amount, a day outside 1–31, or an unsupported
    cadence raises. Each envelope reference is resolved to its canonical name
    **once at parse time** so validation and projection can never bind to
    different envelopes.
- **Deterministic projection.** Bills and transfers are expanded into concrete
  dated events inside the closed window `[as_of, through]` (monthly cadence; day
  clamped per month via `calendar.monthrange`). Each envelope's events are walked
  in date order from its current balance; the running minimum and the **first**
  date the balance goes below zero (the at-risk date) and the largest shortfall
  are recorded. The verdict derives from the projected **minimum** balance, not
  the end balance, so an envelope that dips negative mid-horizon and recovers is
  still flagged `at_risk`.
- **Same-day timing is surfaced, not hidden.** On a day where an inflow and a
  bill coincide, the realistic walk applies the inflow first (a scheduled
  allocation is meant to fund that day's bills). But when that same-day inflow is
  *load-bearing* — the day's bills would overdraw without it — the envelope is
  flagged `same_day_funding_dependent` so the user knows solvency depends on
  intraday settlement timing they don't control.
- **Integer cents, honest unknowns.** Balances come from the authoritative
  decimal-string `balance` on each account, parsed to integer cents. If **any**
  of an envelope's accounts has no synced balance, the whole envelope is
  `balance_unknown` and gets no sufficiency verdict — a partially-known envelope
  is never silently treated as if the missing account held zero.
- **Schedule-based, forward-looking — not reconciled against actuals.** Forecast
  projects *scheduled* occurrences in the window; it does not check whether a
  given bill or deposit already posted (that "did it land?" check is the
  subscription audit, Piece E). The two directions are *not* symmetric:
  - An **outflow** (bill) paid early both reduces the current balance and is
    projected again — a pessimistic double-count that can only produce a false
    *at-risk*, never a false *sufficient*. That is the safe direction for a
    safety tool, so it is left as-is.
  - An **inflow** (a scheduled transfer or deposit) that already posted is
    *already reflected* in the synced starting balance; crediting it again would
    optimistically overstate funds and could flip a real shortfall into a false
    *sufficient* — the one unsafe direction. Forecast bounds this without
    reconciling against actuals: it re-walks each envelope crediting **none** of
    its scheduled inflows (the worst case where every projected inflow has
    already arrived) and, when that strips a `sufficient` verdict into a
    shortfall, flags `relies_on_projected_income`. A sufficiency that leans on
    unreconciled future income is therefore surfaced, never silently trusted.

  Forecast is not additive with burn-down's month-to-date actuals.
- **Stable horizon.** The default window is a fixed duration (`as_of` + 60 days)
  rather than a calendar boundary, so the projected occurrence counts do not
  swing with the day the report is run. The resolved `[as_of, through]` window
  and per-envelope occurrence counts are echoed in the output.
- **Output.** Per envelope: current balance, total scheduled in/out, projected
  end and minimum balance, at-risk flag + date + shortfall, the same-day-funding
  flag, the `relies_on_projected_income` flag, and a verdict (`sufficient` /
  `at_risk` / `balance_unknown`). CLI:
  `finance-mcp forecast [--as-of YYYY-MM-DD] [--through YYYY-MM-DD] [--config PATH] [--json]`.

### Piece D — Allocation audit (priority 3) · **built** (`src/finance_mcp/allocation.py`)

Did each scheduled paycheck→envelope transfer fire on its expected day for its
expected amount? `allocation_audit` (pure) expands each `scheduled_transfer`'s
monthly cadence over a window (via the shared `budget_config.monthly_dates`) and
matches every occurrence against actual money movement; `allocation_report` is
the archive-loading wrapper.

- **Evidence depends on kind.** An *internal* transfer (names a `from` envelope)
  is satisfied only by a **reconciled** transfer link (`confirmed`/`inferred`)
  whose debit account maps to the source envelope and whose credit account maps
  to the destination. An *external* transfer (direct deposit, no `from`) is
  satisfied by a real credit posted to a destination-envelope account that is
  not itself a transfer leg.
- **Per-occurrence status:** `on_time` / `early` / `late` (with `drift_days`,
  dated by when the money *landed* — the credit leg) / `wrong_amount`
  (right envelopes and within tolerance, amount differs) / `missing`.
- **Matching is greedy + deterministic.** Occurrences match earliest-expected
  first; each occurrence consumes at most one actual and each actual at most
  once; an exact-amount actual is preferred over a same-envelope near-date
  mismatch; anything beyond `day_tolerance` (default 7 days — safe against the
  ~30-day monthly cadence) is left `missing` rather than force-matched to the
  wrong month.
- **Limitation (intentional):** a needs-confirm (`unconfirmed`) link is **not**
  counted as fired. A genuinely-ambiguous allocation surfaces here as `missing`
  rather than being silently credited to a possibly-wrong envelope; the user
  resolves it in the confirm surface (Piece 5) first.

### Piece E — Subscription audit

Two scripted outputs the assistant reasons over:

1. **Expected-but-missing** — a tracked recurring charge (known merchant, amount,
   cadence) that did not post in its expected window. Fully deterministic.
2. **Candidate-new** — recurring-looking merchants (repeating at a stable amount
   and cadence) that are **not** in the tracked set. The script surfaces the
   candidates; the LLM judges whether each is really a new subscription.

## Scripted vs. LLM boundary

| Capability | Owner | Why |
|---|---|---|
| Planned-vs-actual math (burn-down) | Scripted | Pure arithmetic over reconciled data. |
| Transfer-fired allocation audit | Scripted | Deterministic schedule-vs-actual comparison. |
| Sufficiency / forecast | Scripted | Deterministic projection over the calendar. |
| Expected recurring charge missing | Scripted | We know the expected merchant, amount, and day — a reliable alert shouldn't depend on an LLM. |
| Are there *new* untracked subscriptions? | LLM | Fuzzy pattern recognition + judgment. Script surfaces candidates; assistant decides. |
| Ambiguous classification ("what was this charge?") | LLM | Judgment over context. |

Rule of thumb: **scripts produce structured, auditable facts and candidate
lists; the LLM does the judgment calls.** Decide the owner explicitly for every
new capability.

## Budget config

A single user-supplied JSON file (default `~/.finance-mcp/budget.json`,
`--config` to override) holding:

- **Envelopes**: each binds a human name to one or more stable account ids and an
  optional monthly target. One account belongs to exactly one envelope.
- **Recurring calendar** (added in Piece C): `recurring` bills (paying envelope,
  amount, cadence, due day-of-month) and `scheduled_transfers` (an optional
  source envelope, a destination envelope, amount, cadence, day). An internal
  transfer (with a source) is conserved as a paired debit/credit.

This file is the SSOT for budget *intent*. It lives outside the repo (it is
personal data), alongside the existing finance-mcp credential/cache home, and is
referenced by path. Targets and the calendar are not duplicated anywhere in code.

## Relationship to transfer reconciliation

The transfer-reconciliation track (storage foundation, matching engine, and the
account product-type map) is a prerequisite: burn-down, allocation audit, and
sufficiency all depend on transfers being correctly separated from real spend.
Those pieces are already built and reviewed. Envelope budgeting consumes their
output; it does not modify them.

## Build process

Each piece: design note here if it deviates → implement with unit + end-to-end
tests → full suite green → review until clean → land. No piece is "done" until
its tests pass and it has been reviewed.
