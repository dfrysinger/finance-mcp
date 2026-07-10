# Roadmap — future considerations

Status: **living document** · Last updated: 2026-07-09

Deferred work and larger design decisions that are understood but not yet
scheduled. Each entry captures the problem, what exists today, and the shape of
the durable fix so a future session can pick it up with full context.

## Durable regex safety for category rules

**Tracking:** issue #4 — "Consider a non-backtracking match engine for regex
category rules."

### Problem

Category rules let a user match transactions with a regular expression
(`match_mode="regex"`). Regex on Python's standard `re` engine is
*backtracking*: certain pattern shapes — nested unbounded quantifiers,
overlapping alternations, long runs of unbounded quantifiers — take time that
grows exponentially (or high-order polynomially) in the length of the input
being tested. A pattern only a few dozen characters long can stall for
effectively unbounded time on a single non-matching string. This is the classic
**ReDoS** (regular-expression denial of service) footgun.

Because categorization runs on **every read**, a single dangerous rule stored in
the database would freeze every subsequent pass — a self-inflicted outage, or a
trap if rule creation is ever exposed to another party.

### What exists today (creation-time guard)

`_validate_regex_safety` in `src/finance_mcp/categories.py` screens a pattern
when a rule is created:

- caps the pattern length so the parser only ever sees bounded input;
- walks the parsed pattern tree and rejects backtracking quantifiers that
  enclose another quantifier or an alternation;
- rejects patterns carrying more than a handful of unbounded quantifiers;
- normalizes parser failures (including deep-nesting and oversized-repetition
  errors) into a clean `ValueError` caught by the MCP/CLI error envelope.

This is deliberately **conservative, not a proof**. It blocks the recognizable
dangerous shapes and accepts the safe non-backtracking rewrites (atomic groups
`(?>...)`, possessive quantifiers `a++`). It does not attempt to prove that an
arbitrary accepted pattern cannot backtrack, so a novel dangerous shape could in
principle slip through, and the guard has to keep pace with new footgun shapes
by hand.

### Why this is coupled to the supported Python version

The guard's recommended safe rewrites — atomic groups and possessive
quantifiers — were only added to the standard `re` engine in **Python 3.11**.
Both rule validation and match-time compilation use stdlib `re`, so on Python
3.10 those rewrites can neither be created nor matched: the guard would reject a
user's footgun *and* reject the fix it recommends. For that reason the project's
supported floor is **Python 3.11+**. Fully supporting 3.10 would require the
same third-party engine described below, so the two decisions are linked.

### The durable fix

Replace the backtracking `re` engine with a **non-backtracking** matcher for
category-rule evaluation — for example the third-party `regex` module in a
linear mode, or an RE2-style engine. A non-backtracking engine is structurally
incapable of the exponential blow-up, so **any** pattern a user supplies is safe
by construction. That removes the entire risk class and retires the hand-tuned
heuristic guard (which could then relax to syntax validation only).

**Trade-offs to weigh before scheduling:**

- Adds a third-party dependency (currently the runtime depends only on `mcp`).
- Requires rewiring both the creation path and the match path
  (`_compiled_rules`) onto the new engine, and re-validating that existing
  stored rules behave identically.
- Some `re` syntax and flags differ across engines; existing rules and tests
  must be checked for compatibility.
- Once adopted, the Python floor could return to 3.10 if desired, since the new
  engine would no longer depend on 3.11-only `re` features.

**Decision criteria:** pursue this when rule creation becomes exposed beyond the
single trusted operator, when the heuristic guard starts requiring frequent
hand-patching for new footgun shapes, or when broad Python-version support
becomes a requirement again.

## Separating multiple same-price recurring streams at one merchant

**Tracking:** issue — "Subscription detection can merge two same-merchant,
same-amount monthly streams into one biweekly candidate."

### Problem

Subscription candidate detection groups a merchant's charges by identity, then
splits them into near-equal-amount clusters (`_amount_clusters`, tolerance
`max($0.50, 5%)`) and, within a cluster, keeps any *distinct exact amount* that
recurs on its own as a separate stream (`_recurring_subgroups`). Two
subscriptions at one merchant with **different prices** therefore surface
separately even when their prices are within tolerance.

The remaining gap is two subscriptions at the **same exact amount** billed on
**different days of the month** (e.g. two $10.00 plans, one on the 1st and one
on the 15th). They share one exact-amount subset, so per-amount separation
cannot tell them apart; their combined postings look like a ~14-day cadence, so
the stream is labelled `biweekly`.

### What exists today

`detect_subscriptions` only writes `monthly` candidates as bills; a `biweekly`
candidate is reported under `unsupported_cadence` and **no bill is written**.
For the same-amount/different-day case the observable outcome is "neither
subscription is auto-tracked" — the same net result the pre-broadening detector
produced (its exact-cents merge grouped both into one bucket whose ~14-day
spacing matched no legacy cadence band either, so it surfaced nothing).

The consequence is a spurious `biweekly` entry in the audit's candidate list,
not a wrong persisted bill or a wrong amount.

### The durable fix

Before classifying cadence, detect when a same-merchant, same-amount stream
actually decomposes into multiple regular streams keyed on day-of-month (or
billing cycle), and partition it so each is classified (and offered) on its own.
This wants care: it must not re-fragment a single monthly stream whose posting
day drifts by a few days across months.

**Decision criteria:** pursue when a real account surfaces this pattern (two
same-priced subscriptions at one merchant on different days), or when the
spurious biweekly candidate proves confusing in practice.

## Surfacing a sub-threshold subscription price step at proposal time

**Tracking:** issue #8 — "Surface a sub-threshold subscription price step at
proposal time."

### Problem

When an untracked subscription's price steps up mid-window and the **new** price
has been seen fewer than `min_occurrences` (default 3) times,
`detect_subscriptions` proposes a bill at the **old** price and does not surface
the newer-price charges. Example: `NETFLIX` at $9.99 for three months, then
$10.49 for two — one $9.99 bill is proposed and the two $10.49 charges are
dropped (not surfaced under `skipped`).

### Why the impact is limited

- **Self-heals at proposal time.** Once the new price reaches `min_occurrences`,
  it becomes its own legacy exact stream; the same-keyword bill-layer dedup then
  proposes the bill at the most-recent price and surfaces the old price as a
  `needs_review` "price change or separate subscription" note.
- **Covered once tracked.** If the user accepts the $9.99 bill, the audit's
  `expected_missing` alert flags the bill as overdue (the $9.99 amount no longer
  matches the $10.49 charges), prompting the user to update the amount. (The
  `_tracked_amount_mismatch` price-change note does not fire here — it needs the
  new price to itself recur at `min_occurrences`; below that threshold the
  `expected_missing` signal is what surfaces the drift.)
- **Not a regression.** The pre-broadening detector also dropped a sub-threshold
  recent price step.

### The durable fix

At proposal time, surface same-keyword charges that recur at a different price
below the stream threshold (e.g. seen ≥2 times) as a `needs_review` price-change
note, symmetric to the already-tracked `_tracked_amount_mismatch` path. Choose
the sub-threshold count carefully to avoid noise from one-off amount blips.

**Decision criteria:** pursue when a real account is mid price-transition and the
stale proposal proves confusing, or alongside the multi-stream separation work
above (both are refinements of same-merchant/same-keyword disambiguation).
