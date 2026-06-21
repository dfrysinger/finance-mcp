# Envelope Budgeting — Design

Status: **design locked, building iteratively** · Last updated: 2026-06-20

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

### Piece A — Statement import (foundation)

A CSV importer that reads each supported export schema and writes archive
`transactions` rows.

- **Format adapters.** One small adapter per source schema (per-envelope bank
  export, and each card export). Each adapter maps its columns onto the archive's
  normalized transaction shape. New formats are added as new adapters, not by
  editing a monolith.
- **Stable synthetic ids.** Imported rows get a deterministic id derived from
  `(source, account, date, amount, description)` so re-importing the same file is
  idempotent and so an imported row can be reconciled/deduped against a SimpleFIN
  row for the same transaction.
- **Idempotent upsert.** Re-running import never duplicates and never clobbers a
  hand-assigned category (categories live in their own table).
- **Output.** A richer multi-year `transactions` archive. The existing transfer
  reconciler and product-type map run over it unchanged.

### Piece B — Envelope burn-down (priority 1)

For a given month, per envelope: **planned target vs. actual spend**, where
actual spend = envelope outflows **minus reconciled inter-envelope transfers**
(transfers are allocation flow, not spend — the reconciler already isolates them).

- Output per envelope: target, actual spend, remaining headroom, over/under flag.
- Pure function over reconciled, categorized transactions + the budget config.

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

A single user-supplied file (format TBD during Piece B — likely JSON) holding:

- **Envelopes**: stable account identifier → human envelope name + role.
- **Monthly targets**: per envelope.
- **Recurring calendar**: expected charges (merchant pattern, amount, cadence,
  day-of-month window, paying envelope) and scheduled paycheck→envelope transfers.

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
