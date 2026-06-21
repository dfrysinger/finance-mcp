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

### Piece E — Subscription audit · **built** (`src/finance_mcp/subscription.py`)

Did each tracked recurring charge post when it should have, and what
recurring-looking merchants aren't tracked yet? `subscription_audit` (pure)
produces two structured outputs the assistant reasons over; `subscription_report`
is the archive-loading wrapper.

A tracked recurring charge is a `recurring` bill in the budget config. To make
"missing" alerts reliable rather than noisy, a bill may carry an optional
`match` keyword — case-insensitive, normalized into tokens and tested as a
token-subset against a transaction's merchant identity (description / payee;
memo only when it is the only text present) — that pins the bill to its
merchant. The keyword must contain at least one letter (a digits-only keyword
like `"76"` normalizes to no token and would match nothing, so it is rejected at
config-parse time).

1. **Expected-but-missing** (deterministic). Each bill's monthly cadence is
   expanded over the window via the shared `budget_config.monthly_dates`. An
   occurrence is *matched* when a debit lands within `day_tolerance` days for the
   expected amount (within `amount_tolerance_cents`) and — when the bill has a
   `match` keyword — whose merchant text contains it; without a keyword the debit
   must instead fall on one of the bill's envelope's accounts. Matching is greedy
   and deterministic (earliest occurrence first; each debit consumed once;
   exact-amount preferred). An unmatched occurrence is reported `missing` only
   once it is genuinely overdue — an occurrence within `grace_days` of the window
   end is skipped (it may still post), so the audit never cries wolf on a charge
   that simply isn't due yet.
   - **No-keyword fallback.** A bill without a `match` keyword is matched by its
     envelope→account binding instead (a debit on one of the envelope's accounts
     for the expected amount near the day). Pinning the merchant with a `match`
     keyword is strictly more reliable — it survives the charge landing on a
     different card — so it is recommended for any subscription you want a
     dependable missing-charge alert on.
2. **Candidate-new** (script surfaces, LLM judges). Debits not consumed by any
   tracked bill — and whose merchant doesn't match a tracked bill's keyword — are
   grouped by `(normalized merchant, exact amount)`. A group with at least
   `min_occurrences` debits whose median spacing lands in a recurring band
   (weekly ≈ 7, monthly ≈ 30, yearly ≈ 365) is surfaced as a candidate with its
   amount, occurrence count, first/last seen, median interval, inferred cadence,
   and a few sample descriptions. The script makes no judgment call — it hands
   the assistant an auditable candidate list to reason over.

- **Window.** `subscription_report` defaults to a 365-day lookback so a monthly
  subscription reliably clears `min_occurrences`. **Limitation:** an annual
  subscription posts at most once in that window and so will not surface as a
  candidate (it needs multiple years of history); a tracked annual bill is still
  checked for expected-but-missing normally.
- **Money / cadence.** Integer cents throughout, rendered to dollars only at the
  report edge; amounts are grouped on exact cents (a stable price is the
  subscription signal), so a mid-window price change splits a merchant into two
  groups.
- **Candidate grouping (merchant identity + subset-merge).** Both bill matching
  and candidate suppression use the merchant-identity tokens (description +
  payee, normalized; memo only when it is the only text present), so a tracked
  brand in the payee still pins its bill and the catch-all `memo` column
  (transaction type, check number, cardholder, …) can't satisfy a bill and hide
  a genuinely-missing charge. Candidate *grouping* buckets debits by (amount,
  identity-token set), then defragments in two cadence-aware phases so a merge can
  only ever *help*, never *demote*. Phase 1 emits any bucket that already recurs
  on its own (meets the occurrence threshold with a non-irregular cadence) and
  never folds it into a superset. Phase 2 subset-merges only the remaining
  sub-threshold buckets — folding each token set into its **unique maximal
  superset** at the same amount — and keeps a merged group only if it now recurs.
  This reunites one merchant whose identity tokens vary across charges as a subset
  *chain* — an auxiliary field populated on only some rows (`{netflix}` ⊆
  `{netflix, com}`), or a per-charge auth code / location that inflates the set
  (`{spotify}` ⊆ `{spotify, new, york, …}`, `{com, wix}` ⊆ `{com, wix, www}` —
  both observed in real data) — so the merchant clears the threshold instead of
  fragmenting below it, while a one-off off-cadence remnant (`{netflix, promo}`)
  can neither demote an already-recurring merchant nor fabricate one. A token set
  that sits under *two or more incomparable* maximal supersets is genuinely
  ambiguous (a bare `{pos, purchase}` under both `{pos, purchase, hulu}` and
  `{pos, purchase, disney}`) and is left standalone, so two distinct merchants
  never merge. A merchant named only in the payee under a numeric/junk description
  still groups (the numeric tokens normalize away) and is labeled from the field
  that carries a real merchant token.

  *Accepted limitation (advisory output only):* the subset-merge defragments a
  merchant only when its identity token sets form a *chain* (totally ordered by
  subset). A merchant whose descriptor varies in *incomparable* ways across rows —
  e.g. `SPOTIFY USA`, `SPOTIFY COM`, `SPOTIFY NY` normalizing to the antichain
  `{spotify, usa}` / `{spotify, com}` / `{spotify, ny}` — produces no subset
  relationships, so its rows do not merge and it can fall below the occurrence
  threshold. Reuniting an antichain requires weighting tokens by how
  *distinctive* they are (IDF / stopword clustering), a tunable heuristic that
  trades one class of false-merge/false-split error for another and cannot be made
  threshold-free; it would expand, not shrink, the scripted surface. Because
  `candidate_new` is an advisory list an assistant reviews — never the
  deterministic billing-miss alert, which does not use this grouping — this fuzzy
  defragmentation is deliberately delegated to the LLM assistant per the
  scripted-vs-LLM boundary below.

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

## Piece F — Surfaces (CLI + MCP) · **built** (`cli.py`, `server.py`, `reconcile.py`, `archive.py`)

Every prior piece ships a pure module plus an archive-loading `*_report`
wrapper, deliberately *without* a user-facing surface — surfacing is this piece.
A person drives the system from the terminal; the Copilot assistant drives the
same engine over MCP. The two surfaces are kept at parity so neither path is a
second-class citizen.

- **Reports are read-only mirrors.** `allocation` / `subscriptions` (CLI) and
  `allocation_audit_report` / `subscription_audit_report` / `budget_burndown` /
  `budget_forecast` (MCP) load the budget config and durable archive and render
  the existing report dicts. They add no new logic — same numbers, two surfaces.
  Both accept `--json` (CLI) / return raw dicts (MCP) so the assistant gets the
  full structured output, not just the human text.
- **Transfer confirm flow.** Reconciliation infers the hidden counterparty of
  each internal transfer, but a *changed* pairing is downgraded to needs-confirm
  for the user to review (see below). The surface closes that loop:
  - `transfers` / `list_transfers` render every link as
    `from_account -> to_account $amount [why]`, recovering the account names the
    raw feed hides and carrying the `explanation` that records *why* the pairing
    was drawn. Needs-confirm rows sort first. An optional status filter narrows
    to one lifecycle state; an unknown status yields an empty list rather than
    silently returning everything.
  - `confirm <link_id>` / `confirm_transfer(link_id)` promote one link to
    `confirmed`. A confirmed link is user-authoritative: subsequent reconciles
    exclude its legs from the matcher, so the decision is never silently
    recomputed. Confirm is idempotent and refuses a single-leg `unmatched` row
    (there is no counterparty to authorize).
  - `reconcile` / `reconcile_transfers` re-run the matcher idempotently so the
    link set reflects the latest sync before the user reviews it.
- **Surface owns no logic.** `confirm` resolves to `archive.confirm_transfer_link`
  (the only mutation) and `transfers` to `reconcile.transfers_view` (a pure
  join). The CLI and MCP layers are thin: parse, call, render. This keeps the
  audited engine the single source of truth and the surfaces independently
  testable.

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
