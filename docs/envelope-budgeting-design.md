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

### Piece C — Sufficiency / forecast (priority 2)

For each envelope: current balance + scheduled inflows for the period vs. known
upcoming bills from the recurring calendar → "will it cover what's coming, and by
when is it at risk." Deterministic projection over the calendar.

### Piece D — Allocation audit (priority 3)

Did each scheduled paycheck→envelope transfer fire on its expected day for its
expected amount? Compares the recurring-transfer schedule in the budget config
against reconciled transfer legs. Surfaces missing / late / wrong-amount
allocations.

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
- **Recurring calendar** (added in later pieces): expected charges (merchant
  pattern, amount, cadence, day-of-month window, paying envelope) and scheduled
  paycheck→envelope transfers.

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
