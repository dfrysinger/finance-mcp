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
