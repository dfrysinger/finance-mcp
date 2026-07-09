"""Transaction categorization: a rules engine + manual overrides.

Categories are derived, not stored on the transaction row, so they survive every
re-sync. Resolution order for a transaction's effective category:

1. a manual override (``transaction_categories``), else
2. a debt-account pin: a posting on a configured debt/loan account is debt
   activity, not income or spending (see ``apply_categories``), else
3. the first matching rule (``category_rules``, lowest ``priority`` wins), else
4. ``"Uncategorized"``.

Rules and overrides carry an ``is_transfer`` flag. Internal transfers and
credit-card payments are not real spending, so budgets exclude them by default.
"""

from __future__ import annotations

import math
import re
import sqlite3
from datetime import datetime, timezone

UNCATEGORIZED = "Uncategorized"
LOAN_PAYMENT = "Loan Payment"

# Categories that represent money earned (not a refund of a purchase). Spending
# views exclude these the same way they exclude transfers, so income never masks
# or offsets real spend. A transfer is never income (the ``is_transfer`` flag
# wins), and an inflow that merely refunds a purchase keeps its spending category
# so it can net against that envelope instead of being dropped as income.
INCOME_CATEGORIES = frozenset({"Income", "Investment Income"})


def is_income_category(category: str | None, is_transfer: bool) -> bool:
    """True when ``category`` is income and the row is not a transfer.

    ``is_transfer`` dominates: an internal move labeled with an income-like
    category is still a transfer, not income.
    """
    return not is_transfer and category in INCOME_CATEGORIES

# Starter rules, seeded from the user's real merchants plus common ones. Patterns
# are case-insensitive substrings. Transfers/payments are flagged is_transfer=1
# and given high priority so they win over any incidental merchant match.
DEFAULT_RULES: list[tuple[str, str, str, int, int]] = [
    # (pattern, field, category, is_transfer, priority)
    ("transfer to", "any", "Transfer", 1, 10),
    ("transfer from", "any", "Transfer", 1, 10),
    ("online transfer", "any", "Transfer", 1, 10),
    # Credit-card balance payoffs (is_transfer=1 so the payoff is not double-counted
    # on top of the purchases it settles). Only unambiguous card-payment descriptors
    # belong here. NOT included: generic "autopay"/"e-payment"/"automatic payment" —
    # utility, insurance, mortgage, and loan ACH debits from checking are commonly
    # labeled that way, so a generic match here at top priority would shadow the
    # specific merchant rules and hide those real bills. They stay Uncategorized
    # (real spend). "payment - thank" ("Payment - Thank You") is unambiguous.
    ("payment - thank", "any", "Credit Card Payment", 1, 10),
    ("nordstrom pymt", "any", "Credit Card Payment", 1, 10),
    # P2P apps are dual-use (rent, contractors, reimbursements) — counting them
    # as spend by default is the honest budgeting choice; the user can override a
    # specific reimbursement. Do NOT mark is_transfer here or real outflow hides.
    # Priority 5 wins over the generic "transfer to/from" rules (priority 10) so a
    # "ZELLE TRANSFER TO JOHN" description stays a P2P payment, not a transfer.
    ("venmo", "any", "P2P Payment", 0, 5),
    ("zelle", "any", "P2P Payment", 0, 5),
    ("cash app", "any", "P2P Payment", 0, 5),
    # Income
    ("payroll", "any", "Income", 0, 20),
    ("direct dep", "any", "Income", 0, 20),
    # Common standard income sources (generic, not merchant-specific). These are
    # earnings/benefits, never a refund of a purchase, so the spending view
    # excludes them once classified. Patterns must be safe as case-insensitive
    # substrings: avoid short tokens that appear inside unrelated merchant words
    # (e.g. "pension" would match "suspension" and bury real auto-repair spend).
    ("unemployment", "any", "Income", 0, 20),
    ("social security", "any", "Income", 0, 20),
    ("dividend", "any", "Investment Income", 0, 20),
    ("interest paid", "any", "Investment Income", 0, 20),
    ("interest earned", "any", "Investment Income", 0, 20),
    ("principal interest", "any", "Investment Income", 0, 20),
    ("reinvestment", "any", "Investment Income", 0, 20),
    ("realizedgainloss", "any", "Investment Income", 0, 20),
    # Account-to-account internal movements (not spending). Only unambiguously
    # internal descriptions belong here. NOT included: bare "withdrawal"/
    # "contribution" (collide with real ATM/cash spend and charitable giving) and
    # "deposit mobile banking" (typically a real check deposit = inbound income,
    # not an internal move). Those stay Uncategorized unless the user adds a rule.
    ("funds tran", "any", "Transfer", 1, 12),
    ("overdraft to", "any", "Transfer", 1, 12),
    # Groceries
    ("harmons", "any", "Groceries", 0, 50),
    ("costco whse", "any", "Groceries", 0, 50),
    ("costco gas", "any", "Gas", 0, 45),
    ("wal-mart", "any", "Groceries", 0, 50),
    ("walmart", "any", "Groceries", 0, 50),
    ("target t-", "any", "Groceries", 0, 50),
    ("smith's", "any", "Groceries", 0, 50),
    ("trader joe", "any", "Groceries", 0, 50),
    ("whole foods", "any", "Groceries", 0, 50),
    ("winco", "any", "Groceries", 0, 50),
    ("sprouts", "any", "Groceries", 0, 50),
    ("kroger", "any", "Groceries", 0, 50),
    # Dining
    ("mcdonald", "any", "Dining", 0, 50),
    ("wendys", "any", "Dining", 0, 50),
    ("wendy's", "any", "Dining", 0, 50),
    ("freddy's", "any", "Dining", 0, 50),
    ("freddys", "any", "Dining", 0, 50),
    ("marcos pizza", "any", "Dining", 0, 50),
    ("betos", "any", "Dining", 0, 50),
    ("beto's", "any", "Dining", 0, 50),
    ("tst ", "any", "Dining", 0, 55),
    ("tst*", "any", "Dining", 0, 55),
    ("crumbl", "any", "Dining", 0, 50),
    ("swig", "any", "Dining", 0, 50),
    ("doordash", "any", "Dining", 0, 50),
    ("uber eats", "any", "Dining", 0, 50),
    ("starbucks", "any", "Dining", 0, 50),
    ("chick-fil", "any", "Dining", 0, 50),
    ("chipotle", "any", "Dining", 0, 50),
    ("domino", "any", "Dining", 0, 50),
    ("subway", "any", "Dining", 0, 50),
    ("panda express", "any", "Dining", 0, 50),
    ("pizza", "any", "Dining", 0, 70),
    ("restaurant", "any", "Dining", 0, 70),
    # Shopping
    ("amazon", "any", "Shopping", 0, 60),
    ("amzn", "any", "Shopping", 0, 60),
    ("nordstrom", "any", "Shopping", 0, 60),
    ("best buy", "any", "Shopping", 0, 60),
    ("home depot", "any", "Home Improvement", 0, 60),
    ("lowes", "any", "Home Improvement", 0, 60),
    ("ebay", "any", "Shopping", 0, 60),
    ("etsy", "any", "Shopping", 0, 60),
    ("aliexpress", "any", "Shopping", 0, 60),
    ("bambulab", "any", "Shopping", 0, 60),
    ("ikea", "any", "Home Improvement", 0, 60),
    # Software / subscriptions
    ("apple.com/bill", "any", "Subscriptions", 0, 50),
    ("github", "any", "Software", 0, 50),
    ("replit", "any", "Software", 0, 50),
    ("openai", "any", "Software", 0, 50),
    ("anthropic", "any", "Software", 0, 50),
    ("netflix", "any", "Subscriptions", 0, 50),
    ("spotify", "any", "Subscriptions", 0, 50),
    ("hulu", "any", "Subscriptions", 0, 50),
    ("disney plus", "any", "Subscriptions", 0, 50),
    ("youtube", "any", "Subscriptions", 0, 50),
    ("adobe", "any", "Subscriptions", 0, 50),
    ("google one", "any", "Subscriptions", 0, 50),
    ("patreon", "any", "Subscriptions", 0, 50),
    ("figma", "any", "Software", 0, 50),
    ("sketch", "any", "Software", 0, 50),
    ("termius", "any", "Software", 0, 50),
    # Gas / transportation
    ("chevron", "any", "Gas", 0, 50),
    ("shell oil", "any", "Gas", 0, 50),
    ("exxon", "any", "Gas", 0, 50),
    ("maverik", "any", "Gas", 0, 50),
    ("sinclair", "any", "Gas", 0, 50),
    ("uber", "any", "Transportation", 0, 65),
    ("lyft", "any", "Transportation", 0, 65),
    ("delta air", "any", "Travel", 0, 50),
    ("southwest air", "any", "Travel", 0, 50),
    # Utilities
    ("dominion energy", "any", "Utilities", 0, 50),
    ("rocky mountain power", "any", "Utilities", 0, 50),
    ("comcast", "any", "Utilities", 0, 50),
    ("xfinity", "any", "Utilities", 0, 50),
    ("centurylink", "any", "Utilities", 0, 50),
    ("google fiber", "any", "Utilities", 0, 50),
    # Health
    ("pharmacy", "any", "Health", 0, 60),
    ("walgreens", "any", "Health", 0, 60),
    ("cvs", "any", "Health", 0, 60),
    ("intermountain", "any", "Health", 0, 60),
    ("dental", "any", "Health", 0, 60),
]


# Default rules removed in later versions. An archive seeded before a default was
# pruned still holds the stale row, and ``seed_default_rules`` early-returns once
# an archive is seeded — so without an explicit cleanup the obsolete rule lingers
# and keeps mis-categorizing spend. Each entry is the exact original default
# signature; only an unmodified default (``account_id IS NULL`` and every other
# column matching) is deleted, so a user's own same-pattern rule is preserved.
# Bump ``_OBSOLETE_DEFAULTS_VERSION`` whenever this list grows.
_OBSOLETE_DEFAULT_RULES: list[tuple[str, str, str, int, int]] = [
    # "pension" matched the substring inside "suspension", burying auto-repair
    # spend as Income. Replaced by collision-safe income patterns.
    ("pension", "any", "Income", 0, 20),
]
_OBSOLETE_DEFAULTS_VERSION = 1


# Default rules added in later versions. ``seed_default_rules`` only inserts the
# starter set on first seed (it early-returns once the ``rules_seeded`` sentinel
# exists, so a user who curates the defaults away does not get them back). That
# same early-return means a NEW default added to ``DEFAULT_RULES`` never reaches
# an archive seeded before it existed — so the new rule silently does nothing for
# every existing user. Each entry here is backfilled once into already-seeded
# archives, gated by the ``new_default_rules_version`` meta sentinel.
#
# The trailing int is the version that introduced the rule. The backfill inserts
# only rules whose introduced-version is greater than the archive's already-applied
# version, and only when the rule's pattern is absent (so a user's own same-pattern
# rule is never duplicated). The introduced-version gate means a later
# ``_NEW_DEFAULTS_VERSION`` bump (to add a brand-new rule) does NOT re-evaluate —
# and therefore never resurrects — an earlier backfilled rule the user has since
# deleted.
#
# Known limitation for the v1 (income) entries: backfilling at ``applied == 0`` is
# required to reach genuinely legacy archives seeded before the income rules
# existed — that is the whole point. But the very first income release seeded those
# rules via the normal path WITHOUT writing this sentinel, so an archive freshly
# seeded by that one release whose user then deleted an income rule is, at
# ``applied == 0``, indistinguishable from a legacy archive — and the backfill will
# restore the rule once. This window is closed going forward: a fresh seed by this
# version records ``new_default_rules_version`` (see ``_insert_new_default_rules``),
# so any later deletion is preserved. Do NOT extend this rationalization to future
# additions — a rule added in a release that also ships this sentinel has no such
# ambiguity and must carry the correct introduced-version. Bump
# ``_NEW_DEFAULTS_VERSION`` whenever this list grows, tagging each appended rule
# with that new version.
_NEW_DEFAULT_RULES: list[tuple[str, str, str, int, int, int]] = [
    # Income flag (parallel to transfers) shipped after the starter set, so
    # archives seeded earlier never received these and kept counting benefit
    # deposits as negative spending. Both patterns are collision-safe substrings.
    ("unemployment", "any", "Income", 0, 20, 1),
    ("social security", "any", "Income", 0, 20, 1),
]
_NEW_DEFAULTS_VERSION = 1


def _now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _prune_obsolete_default_rules(conn: sqlite3.Connection) -> None:
    """Delete superseded default rules from an already-seeded archive (once).

    Runs inside the caller's open transaction (``seed_default_rules`` opens it).
    Idempotent: a ``meta`` version key records the highest obsolete-set applied,
    so this is a no-op after the first run. Only an exact, unmodified default
    signature with ``account_id IS NULL`` is removed, so a user's customized
    same-pattern rule is never touched.
    """
    row = conn.execute(
        "SELECT value FROM meta WHERE key='obsolete_default_rules_version'"
    ).fetchone()
    applied = int(row[0]) if row is not None and str(row[0]).isdigit() else 0
    if applied >= _OBSOLETE_DEFAULTS_VERSION:
        return
    for pat, field, cat, transfer, prio in _OBSOLETE_DEFAULT_RULES:
        conn.execute(
            "DELETE FROM category_rules WHERE pattern=? AND field=? AND category=? "
            "AND is_transfer=? AND priority=? AND account_id IS NULL",
            (pat, field, cat, transfer, prio),
        )
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) "
        "VALUES ('obsolete_default_rules_version', ?)",
        (str(_OBSOLETE_DEFAULTS_VERSION),),
    )


def _insert_new_default_rules(conn: sqlite3.Connection, *, already_seeded: bool) -> int:
    """Backfill newly-added default rules into an already-seeded archive (once).

    Runs inside the caller's open transaction (``seed_default_rules`` opens it).
    Idempotent: a ``meta`` version key records the highest new-default set applied,
    so this is a no-op after the first run. Returns the number of rules inserted.

    Only an ``already_seeded`` archive is backfilled here. A fresh archive (no
    ``rules_seeded`` sentinel) gets these patterns from the normal seed insert in
    ``seed_default_rules``; backfilling them here too would double-insert and throw
    off that function's inserted-count return. In the fresh case this still records
    the version sentinel so the backfill never fires later. Each new default is
    inserted only when (a) its introduced-version is newer than the archive's
    already-applied version — so a later version bump never re-evaluates, and thus
    never resurrects, an earlier backfilled rule the user has since deleted — and
    (b) no rule with that pattern already exists, so a user's own same-pattern rule
    is never duplicated.
    """
    row = conn.execute(
        "SELECT value FROM meta WHERE key='new_default_rules_version'"
    ).fetchone()
    applied = int(row[0]) if row is not None and str(row[0]).isdigit() else 0
    if applied >= _NEW_DEFAULTS_VERSION:
        return 0
    inserted = 0
    if already_seeded:
        existing = {
            r[0] for r in conn.execute("SELECT pattern FROM category_rules").fetchall()
        }
        now = _now()
        to_insert = [
            (p, f, c, t, pr, now)
            for (p, f, c, t, pr, added_in) in _NEW_DEFAULT_RULES
            if added_in > applied and p not in existing
        ]
        if to_insert:
            conn.executemany(
                "INSERT INTO category_rules (pattern, field, category, is_transfer, priority, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                to_insert,
            )
            inserted = len(to_insert)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) "
        "VALUES ('new_default_rules_version', ?)",
        (str(_NEW_DEFAULTS_VERSION),),
    )
    return inserted


def seed_default_rules(conn: sqlite3.Connection, *, force: bool = False) -> int:
    """Fill in the starter rule set, inserting only defaults not already present.

    Seeding is tracked by a ``meta`` sentinel, not by row count, so a user who
    deliberately deletes the defaults to curate their own set does NOT get them
    re-inserted on the next read. When the sentinel is absent (a fresh or legacy
    archive), every default whose pattern is not already present is inserted —
    this completes a partially-populated archive without duplicating a user's own
    rules, and never leaves the defaults missing just because an unrelated custom
    rule was added first. ``force=True`` re-fills missing defaults regardless of
    the sentinel (used by ``categorize --reseed``).

    Returns the number of default rules actually inserted.
    """
    # Acquire the write lock up front (BEGIN IMMEDIATE) so the sentinel check,
    # existing-pattern scan, inserts, and sentinel write are one atomic unit. Two
    # callers seeding a brand-new archive concurrently (e.g. the CLI and the MCP
    # server) would otherwise both see no sentinel and double-insert every default;
    # the duplicates then survive a single remove_rule, continuing to hide spend.
    # Refuse (rather than silently commit) if the caller already holds a
    # transaction — committing work we did not open could make a caller's
    # partial writes durable past their own rollback.
    if conn.in_transaction:
        raise RuntimeError(
            "seed_default_rules must run outside a caller-managed transaction; "
            "it opens its own BEGIN IMMEDIATE write transaction"
        )
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Remove any default rule that has since been superseded, even when the
        # archive is already seeded (the early-return below would otherwise skip
        # it). Guarded to run once. Must precede the sentinel check.
        _prune_obsolete_default_rules(conn)
        already = conn.execute(
            "SELECT value FROM meta WHERE key='rules_seeded'"
        ).fetchone()
        # Symmetric to the prune: backfill defaults added after this archive was
        # seeded, which the sentinel early-return below would otherwise skip,
        # leaving the new rule inert for every existing user. Guarded to run once.
        # Only an already-seeded archive needs this; a fresh archive gets the same
        # patterns from the normal insert below.
        backfilled = _insert_new_default_rules(conn, already_seeded=already is not None)
        if not force:
            if already is not None:
                conn.commit()
                return backfilled
        existing = {
            row[0]
            for row in conn.execute("SELECT pattern FROM category_rules").fetchall()
        }
        now = _now()
        to_insert = [
            (p, f, c, t, pr, now)
            for (p, f, c, t, pr) in DEFAULT_RULES
            if p not in existing
        ]
        if to_insert:
            conn.executemany(
                "INSERT INTO category_rules (pattern, field, category, is_transfer, priority, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                to_insert,
            )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('rules_seeded', ?)",
            (now,),
        )
        conn.commit()
        return len(to_insert) + backfilled
    except Exception:
        conn.rollback()
        raise


def _coerce_amount_bound(value: object) -> float:
    """Coerce a user-supplied amount bound to a float, raising ``ValueError``
    for anything that is not a real number.

    ``bool`` is rejected outright (``True``/``False`` as a money magnitude is a
    caller bug, and ``float(True)`` would silently store ``1.0``). ``float()``
    raises ``TypeError`` for non-numeric, non-string types (e.g. a JSON array
    from an MCP caller, where the ``float | None`` annotation is not
    runtime-enforced); normalize that to ``ValueError`` so the entrypoint's
    ``except ValueError`` handler catches it instead of crashing. Mirrors
    ``_coerce_day_bound``.
    """
    if isinstance(value, bool):
        raise ValueError("amount bounds must be numbers, not booleans")
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("amount bounds must be finite numbers") from exc


def _validate_amount_bounds(
    amount_min: float | None, amount_max: float | None
) -> tuple[float | None, float | None]:
    lo = None if amount_min is None else _coerce_amount_bound(amount_min)
    hi = None if amount_max is None else _coerce_amount_bound(amount_max)
    for v in (lo, hi):
        if v is None:
            continue
        if not math.isfinite(v):
            # NaN/inf slip past the < 0 and lo > hi checks below (every NaN
            # comparison is False), so they would be stored and then silently
            # neuter the predicate at match time. Reject them here.
            raise ValueError("amount bounds must be finite numbers")
        if v < 0:
            raise ValueError("amount bounds are magnitudes and must be >= 0")
    if lo is not None and hi is not None and lo > hi:
        raise ValueError("amount_min must not exceed amount_max")
    return lo, hi


def _coerce_day_bound(value: object) -> int:
    """Coerce a user-supplied day bound to an int in principle, raising
    ``ValueError`` for anything that is not an exact whole number.

    Callers may pass a plain ``int``, but a programmatic/MCP caller can also pass
    a ``float``, ``Decimal``, or numeric string. ``int(value)`` would silently
    truncate a fractional value and would raise ``OverflowError`` (not
    ``ValueError``) for ``inf`` — escaping the caller's error handling. Normalize
    every reject to ``ValueError`` and refuse fractional / non-finite inputs.
    ``bool`` is rejected outright: ``True``/``False`` as a day is a caller bug.
    """
    if isinstance(value, bool):
        raise ValueError("day bounds must be whole numbers, not booleans")
    if isinstance(value, int):
        return value
    try:
        f = float(value)  # float, Decimal, numeric str
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("day bounds must be integers between 1 and 31") from exc
    if not math.isfinite(f):
        raise ValueError("day bounds must be finite integers")
    if not f.is_integer():
        raise ValueError("day bounds must be whole numbers")
    return int(f)


def _validate_day_bounds(
    day_min: int | None, day_max: int | None
) -> tuple[int | None, int | None]:
    out: list[int | None] = []
    for v in (day_min, day_max):
        if v is None:
            out.append(None)
            continue
        iv = _coerce_day_bound(v)
        if not 1 <= iv <= 31:
            raise ValueError("day bounds must be between 1 and 31")
        out.append(iv)
    lo, hi = out
    if lo is not None and hi is not None and lo > hi:
        raise ValueError("day_min must not exceed day_max")
    return lo, hi


_MAX_REGEX_PATTERN_LEN = 200

# Two classic catastrophic-backtracking footguns compile cleanly but can take
# exponential time on a non-matching input — which, because categorization runs
# on every read, would stall every subsequent pass:
#   1. A backtracking quantifier whose body itself contains a quantifier:
#      (a+)+, (a*)*, (.{1,9})+ ...
#   2. A backtracking quantifier whose body contains an alternation: (a|a)+,
#      (a|ab)* ... (the overlap between alternatives is what backtracks; we
#      reject the whole quantified-alternation shape conservatively rather than
#      try to prove overlap).
# Both are detected by walking the *parsed* pattern tree rather than matching
# the pattern source text: a source-text heuristic that scans for "(...)<quant>"
# is defeated by an extra wrapping group (e.g. ((a|a))+), whereas the parse tree
# exposes the same nested structure no matter how many groups wrap it.

try:  # Python 3.11+ exposes the regex parser as re._parser
    from re import _parser as _re_parser
except ImportError:  # pragma: no cover - Python < 3.11 fallback
    import sre_parse as _re_parser

_SubPattern = _re_parser.SubPattern
_MAXREPEAT = _re_parser.MAXREPEAT

# Cap on the number of *unbounded* greedy/lazy quantifiers (``*``/``+``/``{n,}``)
# in one pattern. Even without nesting, several sequential unbounded quantifiers
# over overlapping character sets (``.*.*.*x``) backtrack polynomially with a
# degree equal to the quantifier count, which — on a non-matching transaction
# string, evaluated on every read — grows into a multi-second hang well under
# the length cap. Realistic merchant patterns use only a few (``\s+``, ``\d+``,
# one ``.*``); this bound leaves generous headroom for them while rejecting the
# high-degree shapes that stall categorization. Bounded quantifiers (``?``,
# ``{2,4}``, ``{3}``) and non-backtracking possessive repeats are exempt.
_MAX_UNBOUNDED_REPEATS = 4

# Greedy/lazy quantifiers backtrack; POSSESSIVE_REPEAT ((a+)++) and ATOMIC_GROUP
# ((?>...)) do not, so they are not catastrophic-backtracking risks and must not
# be flagged (rejecting them would block the very rewrite that fixes the risk).
_BACKTRACKING_REPEAT_OPS = ("MAX_REPEAT", "MIN_REPEAT")

# An atomic group or possessive repeat commits its match and cannot be forced to
# backtrack by an enclosing quantifier, so it breaks the nesting chain: structure
# inside it is not driven to catastrophic backtracking by an ancestor repeat
# (e.g. (?>a|aa)+ is safe even though (a|aa)+ is not). The walk still recurses
# into the body so a footgun that is self-contained inside the boundary — like
# (?>(a+)+b) — is still caught on its own.
_NON_BACKTRACKING_BOUNDARY_OPS = ("ATOMIC_GROUP", "POSSESSIVE_REPEAT")


def _nested_subpatterns(av: object) -> list:
    """Return every SubPattern nested anywhere in an opcode's argument tuple.

    Walks generically rather than destructuring by index so it survives the
    per-version differences in opcode argument shape (e.g. SUBPATTERN carries
    flag fields on newer Pythons that older ones omit).
    """
    found: list = []

    def visit(node: object) -> None:
        if isinstance(node, _SubPattern):
            found.append(node)
        elif isinstance(node, (tuple, list)):
            for item in node:
                visit(item)

    visit(av)
    return found


def _check_backtracking(subpattern: object, inside_repeat: bool) -> None:
    """Raise ValueError if a backtracking quantifier encloses another quantifier
    or an alternation, anywhere in the tree (the catastrophic-backtracking core).
    """
    for op, av in subpattern:
        name = getattr(op, "name", str(op))
        is_repeat = name in _BACKTRACKING_REPEAT_OPS
        if inside_repeat and is_repeat:
            raise ValueError(
                "regex pattern has nested quantifiers (e.g. '(a+)+') that can "
                "cause catastrophic backtracking; rewrite it without a "
                "quantified group inside another quantifier"
            )
        if inside_repeat and name == "BRANCH":
            raise ValueError(
                "regex pattern has a quantified alternation (e.g. '(a|a)+') "
                "that can cause catastrophic backtracking; rewrite it without "
                "repeating an alternation group"
            )
        # An atomic group / possessive repeat is a non-backtracking boundary:
        # reset inside_repeat so its contents are not flagged merely for sitting
        # under an ancestor quantifier. Self-contained footguns inside it are
        # still caught because the walk continues into the body.
        if name in _NON_BACKTRACKING_BOUNDARY_OPS:
            child_inside = False
        else:
            child_inside = inside_repeat or is_repeat
        for child in _nested_subpatterns(av):
            _check_backtracking(child, child_inside)


def _count_unbounded_repeats(subpattern: object) -> int:
    """Count backtracking quantifiers with an unbounded upper limit, tree-wide.

    Counts ``MAX_REPEAT``/``MIN_REPEAT`` nodes whose max is ``MAXREPEAT`` (``*``,
    ``+``, ``{n,}``) anywhere in the parsed pattern, including inside atomic
    groups (whose contents still backtrack internally before the group commits).
    ``POSSESSIVE_REPEAT`` nodes do not backtrack, so they are not counted, but
    their bodies are still traversed in case they contain backtracking repeats.
    """
    count = 0
    for op, av in subpattern:
        name = getattr(op, "name", str(op))
        if name in _BACKTRACKING_REPEAT_OPS and isinstance(av, tuple) and (
            len(av) == 3 and av[1] == _MAXREPEAT
        ):
            count += 1
        for child in _nested_subpatterns(av):
            count += _count_unbounded_repeats(child)
    return count


def _validate_regex_safety(raw: str) -> None:
    """Reject regex patterns that are obvious catastrophic-backtracking risks.

    Compilation alone does not bound execution time: a pattern like ``(a+)+$``
    compiles fine but can backtrack exponentially on a non-matching input, and
    because categorization runs on every read, one such stored rule would hang
    every subsequent pass. Cap the pattern length, then walk the parsed pattern
    tree and reject any backtracking quantifier that encloses another quantifier
    or an alternation, plus any pattern carrying more than a handful of unbounded
    quantifiers (whose sequential backtracking is polynomial in that count), so
    the footgun surfaces as a creation-time ``ValueError`` (caught by the
    MCP/CLI error envelope) rather than a silent runtime hang. Conservative, not
    a proof: it rejects the structural footgun shapes (and a few benign
    quantified alternations) without trying to prove non-overlap.

    The length check is first so the parser only ever sees a bounded input;
    parse errors (including ``RecursionError`` on deep nesting and
    ``OverflowError`` on an oversized repetition count) are normalized to
    ``ValueError`` so a malformed pattern never escapes as an unhandled crash.
    """
    if len(raw) > _MAX_REGEX_PATTERN_LEN:
        raise ValueError(
            f"regex pattern is too long (max {_MAX_REGEX_PATTERN_LEN} characters)"
        )
    try:
        parsed = _re_parser.parse(raw)
    except re.error as exc:
        raise ValueError(f"invalid regex pattern: {exc}") from exc
    except RecursionError as exc:
        raise ValueError("regex pattern is too deeply nested") from exc
    except OverflowError as exc:
        raise ValueError(f"invalid regex pattern: {exc}") from exc
    _check_backtracking(parsed, False)
    if _count_unbounded_repeats(parsed) > _MAX_UNBOUNDED_REPEATS:
        raise ValueError(
            "regex pattern has too many unbounded quantifiers "
            f"(max {_MAX_UNBOUNDED_REPEATS}); several sequential '*'/'+' "
            "quantifiers can backtrack for seconds on a non-matching input. "
            "Use bounded quantifiers (e.g. '{1,20}') or a more specific pattern"
        )


def add_rule(
    conn: sqlite3.Connection,
    pattern: str,
    category: str,
    *,
    field: str = "any",
    is_transfer: bool = False,
    priority: int = 100,
    account_id: str | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    day_min: int | None = None,
    day_max: int | None = None,
    match_mode: str = "substring",
) -> int:
    """Add a single rule; returns its rule_id.

    ``account_id`` scopes the rule to one account: when set, the rule matches
    only transactions on that account (so an account-specific override can
    reclassify a generic descriptor — e.g. a loan payment that the bank labels
    only "FUNDS TRAN" — without touching the same descriptor elsewhere). When
    ``None`` the rule applies to every account, as before.

    Beyond the merchant match, a rule may carry optional per-transaction
    predicates; all supplied predicates must hold for the rule to match (AND):

    - ``amount_min`` / ``amount_max`` bound the transaction's amount *magnitude*
      (``abs(amount)``), so ``200``–``350`` matches a $304.76 charge regardless
      of sign. A same-magnitude refund would also match, so pair an amount band
      with a merchant pattern to disambiguate.
    - ``day_min`` / ``day_max`` bound the posted day-of-month (1-31), e.g. a
      mid-month recurring charge.
    - ``match_mode='regex'`` matches ``pattern`` as a case-insensitive regular
      expression (``re.search``) against the description/payee instead of a
      plain substring. This lets a pattern survive inserted tokens like store
      numbers. An invalid regex is rejected here so it fails loudly at creation
      rather than silently never matching.

    All predicates default to "match anything" (NULL / substring), so existing
    callers and rules are unaffected.
    """
    if field not in ("description", "payee", "any"):
        raise ValueError("field must be description, payee, or any")
    if match_mode not in ("substring", "regex"):
        raise ValueError("match_mode must be substring or regex")
    raw = (pattern or "").strip()
    if not raw:
        raise ValueError("pattern must not be empty")
    # Substring patterns are stored lowercased and matched against lowercased
    # transaction text. A regex must keep its original case so tokens like \D or
    # [A-Z] survive being stored; case-insensitivity comes from re.IGNORECASE at
    # match time instead. Validating the regex now turns a malformed pattern
    # into a loud creation-time error rather than a rule that silently matches
    # nothing forever.
    if match_mode == "regex":
        # Validate safety (length + backtracking heuristics) BEFORE compiling so
        # an over-long or deeply nested pattern is rejected cleanly rather than
        # reaching the parser. RecursionError from a pathologically nested but
        # under-length pattern is still normalized to ValueError defensively.
        _validate_regex_safety(raw)
        try:
            re.compile(raw, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid regex pattern: {exc}") from exc
        except RecursionError as exc:
            raise ValueError("regex pattern is too deeply nested") from exc
        except OverflowError as exc:
            raise ValueError(f"invalid regex pattern: {exc}") from exc
        pat = raw
    else:
        pat = raw.lower()
    cat = (category or "").strip()
    if not cat:
        raise ValueError("category must not be empty")
    amt_lo, amt_hi = _validate_amount_bounds(amount_min, amount_max)
    day_lo, day_hi = _validate_day_bounds(day_min, day_max)
    acct = (account_id or "").strip() or None
    cur = conn.execute(
        "INSERT INTO category_rules "
        "(pattern, field, category, is_transfer, priority, account_id, "
        "amount_min, amount_max, day_min, day_max, match_mode, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (pat, field, cat, int(is_transfer), priority, acct,
         amt_lo, amt_hi, day_lo, day_hi, match_mode, _now()),
    )
    conn.commit()
    return cur.lastrowid


def remove_rule(conn: sqlite3.Connection, rule_id: int) -> bool:
    cur = conn.execute("DELETE FROM category_rules WHERE rule_id=?", (rule_id,))
    conn.commit()
    return cur.rowcount > 0


def list_rules(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM category_rules ORDER BY priority, rule_id"
    ).fetchall()
    return [dict(r) for r in rows]


def set_manual_category(
    conn: sqlite3.Connection,
    txn_id: str,
    category: str,
    *,
    is_transfer: bool = False,
) -> None:
    """Pin a category to one transaction (wins over every rule, survives sync).

    Raises ``LookupError`` if ``txn_id`` is not a known transaction, so a typo
    fails loudly instead of silently writing an override that never applies.
    """
    cat = (category or "").strip()
    if not cat:
        raise ValueError("category must not be empty")
    exists = conn.execute(
        "SELECT 1 FROM transactions WHERE id=?", (txn_id,)
    ).fetchone()
    if exists is None:
        # The transaction may currently be served only from the legacy cache
        # fallback (archive.db not yet populated). Accept it if the read path
        # knows it; the override applies once it syncs into the archive.
        from . import store  # local import to avoid an import cycle

        known = any(
            t.get("id") == txn_id for t in store.load_archive_view()["transactions"]
        )
        if not known:
            raise LookupError(f"no transaction with id {txn_id!r}")
    conn.execute(
        "INSERT INTO transaction_categories (txn_id, category, is_transfer, source, updated_at) "
        "VALUES (?, ?, ?, 'manual', ?) "
        "ON CONFLICT(txn_id) DO UPDATE SET category=excluded.category, "
        "is_transfer=excluded.is_transfer, source='manual', updated_at=excluded.updated_at",
        (txn_id, cat, int(is_transfer), _now()),
    )
    conn.commit()


def clear_manual_category(conn: sqlite3.Connection, txn_id: str) -> bool:
    cur = conn.execute("DELETE FROM transaction_categories WHERE txn_id=?", (txn_id,))
    conn.commit()
    return cur.rowcount > 0


def _manual_map(conn: sqlite3.Connection) -> dict[str, tuple[str, bool]]:
    return {
        r["txn_id"]: (r["category"], bool(r["is_transfer"]))
        for r in conn.execute(
            "SELECT txn_id, category, is_transfer FROM transaction_categories"
        ).fetchall()
    }


def _compiled_rules(conn: sqlite3.Connection) -> list[dict]:
    rules = list_rules(conn)
    for r in rules:
        raw_pattern = r["pattern"]
        # A non-string pattern (BLOB / affinity-corrupted hand-edited row) is not
        # an evaluable predicate. Normalize it to None now so every branch below
        # fails closed instead of crashing on bytes in re.compile or `in`.
        pattern = raw_pattern if isinstance(raw_pattern, str) else None
        stored_mode = r.get("match_mode")
        # Only a NULL/missing match_mode (legacy rows predating the column)
        # defaults to substring. An empty string or any other non-regex,
        # non-substring value is a corrupted row and must fail closed.
        mode = "substring" if stored_mode is None else stored_mode
        r["_mode"] = mode
        if pattern is None:
            r["_p"] = ""
            r["_rx"] = None
            continue
        if mode == "regex":
            try:
                r["_rx"] = re.compile(pattern, re.IGNORECASE) if pattern else None
            except re.error:
                # A malformed regex (e.g. hand-edited straight into the DB,
                # bypassing add_rule's validation) must never crash
                # categorization; treat it as a rule that matches nothing.
                r["_rx"] = None
            r["_p"] = ""
        elif mode == "substring":
            r["_p"] = pattern.lower()
            r["_rx"] = None
        else:
            # An unknown match_mode (hand-edited / affinity-corrupted row) is not
            # an evaluable predicate. Fail closed — empty _p makes
            # _match_merchant's substring path return False — rather than
            # silently treating it as a substring match the author never chose.
            r["_p"] = ""
            r["_rx"] = None
    return rules


def usable_merchant(value: object) -> str:
    """Lowercased merchant text for matching, or ``""`` when the field is not a
    usable string.

    ``description``/``payee`` are normally strings, but a malformed cached or
    hand-edited row could hold a truthy non-string (an int, list, or dict).
    Calling ``.lower()`` on those raises ``AttributeError`` and aborts the
    matching pass for the whole batch, not just the bad row. Coercing here keeps
    one malformed transaction from crashing the pass — its merchant predicates
    simply fail to match, mirroring the amount/day fail-closed paths. Shared with
    the red-flag debt-payment matcher so both read paths coerce identically.
    """
    if isinstance(value, str):
        return value.lower()
    return ""


def _match_merchant(rule: dict, desc: str, payee: str) -> bool:
    field = rule["field"]
    # add_rule validates field to description/payee/any, but a hand-edited or
    # corrupted stored row could hold any value. An unrecognized field is not a
    # usable match target, so fail closed rather than silently widening the
    # predicate to the any-field (desc OR payee) branch.
    if field not in ("description", "payee", "any"):
        return False
    if rule.get("_mode") == "regex":
        rx = rule.get("_rx")
        if rx is None:
            # Empty or malformed regex matches nothing (fail closed) rather than
            # falling back to a substring match the author did not ask for.
            return False
        if field == "description":
            return rx.search(desc) is not None
        if field == "payee":
            return rx.search(payee) is not None
        return rx.search(desc) is not None or rx.search(payee) is not None
    pat = rule["_p"]
    if not pat:
        return False
    if field == "description":
        return pat in desc
    if field == "payee":
        return pat in payee
    return pat in desc or pat in payee


def _usable_bound(value: object) -> tuple[bool, float | None]:
    """Classify a stored predicate bound read back from the rules table.

    Returns ``(present, finite_value)``:

    - ``(False, None)`` — no bound (the column was NULL).
    - ``(True, <float>)`` — a usable finite bound.
    - ``(True, None)`` — a bound is present but unusable: NaN, inf, or a
      non-numeric value (e.g. a row hand-edited straight into the DB, putting
      TEXT in a REAL/INTEGER column). The caller must fail closed — a present
      bound that cannot be evaluated must never silently un-bound the predicate,
      and reading it must never crash categorization.
    """
    if value is None:
        return (False, None)
    try:
        f = float(value)
    except (TypeError, ValueError):
        return (True, None)
    if not math.isfinite(f):
        return (True, None)
    return (True, f)


def _match_amount(rule: dict, amount_mag: float | None) -> bool:
    lo_present, lo = _usable_bound(rule.get("amount_min"))
    hi_present, hi = _usable_bound(rule.get("amount_max"))
    if not lo_present and not hi_present:
        return True
    # A present-but-unusable bound (NaN/inf/non-numeric) fails closed rather than
    # silently turning a bounded rule unbounded or crashing the matcher.
    if (lo_present and lo is None) or (hi_present and hi is None):
        return False
    # Amount bounds are magnitudes (>= 0); add_rule rejects negatives, so a
    # negative stored bound only reaches here via a hand-edited/legacy row. Since
    # amount_mag is always >= 0, a negative lower bound would silently never
    # exclude anything — treat an out-of-domain bound as unusable and fail closed,
    # mirroring the day path's 1..31 range guard.
    if (lo is not None and lo < 0) or (hi is not None and hi < 0):
        return False
    if amount_mag is None or not math.isfinite(amount_mag):
        # The rule constrains amount but the transaction's amount is missing or
        # not a usable number — can't verify the predicate, so fail closed.
        return False
    if lo is not None and amount_mag < lo:
        return False
    if hi is not None and amount_mag > hi:
        return False
    return True


def _match_day(rule: dict, day: int | None) -> bool:
    lo_present, lo = _usable_bound(rule.get("day_min"))
    hi_present, hi = _usable_bound(rule.get("day_max"))
    if not lo_present and not hi_present:
        return True
    if (lo_present and lo is None) or (hi_present and hi is None):
        return False
    # Day bounds must be whole days in 1..31. Input validation enforces this, so
    # a fractional or out-of-range stored value only reaches here via a
    # hand-edited/legacy row; it is not a usable day threshold, so fail closed
    # rather than match on a partial or impossible day-of-month.
    if (lo is not None and (not lo.is_integer() or not 1 <= lo <= 31)) or (
        hi is not None and (not hi.is_integer() or not 1 <= hi <= 31)
    ):
        return False
    if day is None:
        return False
    if lo is not None and day < lo:
        return False
    if hi is not None and day > hi:
        return False
    return True


def _usable_magnitude(value: object) -> float | None:
    """Return ``abs(value)`` as a finite float, or ``None`` if the stored amount
    is missing or not a usable number.

    ``amount_float`` is a REAL column, but a malformed cached/hand-edited row
    could hold non-numeric or non-finite content. Coercing it here (rather than
    calling ``abs()`` blindly) keeps one bad transaction from crashing the whole
    categorization pass — amount-constrained rules then fail closed via
    ``_match_amount`` while amount-agnostic rules are unaffected.
    """
    if value is None or isinstance(value, bool):
        # bool is an int subclass: True would coerce to abs(1.0) and silently
        # satisfy an amount band. A boolean is not a usable amount — fail closed.
        return None
    try:
        f = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(f):
        return None
    return abs(f)


def _match(
    rule: dict,
    desc: str,
    payee: str,
    account_id: str | None,
    amount_mag: float | None,
    day: int | None,
) -> bool:
    acct = rule.get("account_id")
    if acct is not None and acct != account_id:
        return False
    # All supplied predicates must hold (AND). Merchant first — it is the
    # cheapest and most selective check.
    if not _match_merchant(rule, desc, payee):
        return False
    if not _match_amount(rule, amount_mag):
        return False
    if not _match_day(rule, day):
        return False
    return True


def _day_of_month(txn: dict) -> int | None:
    """Posted day-of-month (1-31) of a transaction, or None if it has no date.

    Prefers ``posted_ts`` (epoch seconds at midnight UTC, as the importer
    stores it) so the day matches how the rest of the app renders the date,
    then falls back to parsing the ``posted`` / ``transacted_at`` date strings.
    """
    ts = txn.get("posted_ts")
    if ts is not None and not isinstance(ts, bool):
        # bool is an int subclass: int(True) == 1 would resolve to 1970-01-01
        # (day 1) and silently satisfy a day predicate. Skip it and fall back.
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).day
        except (ValueError, OverflowError, OSError, TypeError):
            pass
    for key in ("posted", "transacted_at"):
        v = txn.get(key)
        if v:
            for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
                try:
                    return datetime.strptime(str(v).strip(), fmt).day
                except ValueError:
                    continue
    return None


def apply_categories(
    conn: sqlite3.Connection,
    transactions: list[dict],
    *,
    debt_account_ids: frozenset[str] | set[str] | None = None,
) -> list[dict]:
    """Annotate each transaction with ``category``, ``is_transfer``, ``is_income``, ``category_source``.

    Mutates and returns the same list. Resolution order per transaction:

    1. a manual override (``transaction_categories``) — always wins, else
    2. a debt-account pin: a transaction posting on one of ``debt_account_ids`` is
       debt activity (a payment, returned payment, or interest posting), not income
       or spending. The lender labels these with descriptors like "Principal
       Interest" that an income rule would otherwise match, inflating income; pinning
       them to ``"Loan Payment"`` with ``is_transfer=True`` keeps them out of both
       inflow and outflow totals. The same descriptor on a real brokerage account
       (not a debt) still resolves to its income rule, so this is account-scoped, not
       a blanket suppression. Else
    3. the first matching rule (by priority), else
    4. ``Uncategorized``.

    ``debt_account_ids`` defaults to ``None`` (no debt accounts), preserving the
    prior behavior for callers that do not supply it.
    """
    manual = _manual_map(conn)
    rules = _compiled_rules(conn)
    debts = debt_account_ids or frozenset()
    for txn in transactions:
        tid = txn.get("id")
        if tid in manual:
            cat, transfer = manual[tid]
            txn["category"], txn["is_transfer"], txn["category_source"] = cat, transfer, "manual"
            txn["is_income"] = is_income_category(cat, transfer)
            continue
        if txn.get("account_id") in debts:
            txn["category"], txn["is_transfer"], txn["category_source"] = (
                LOAN_PAYMENT, True, "debt_account",
            )
            txn["is_income"] = False
            continue
        desc = usable_merchant(txn.get("description"))
        payee = usable_merchant(txn.get("payee"))
        account_id = txn.get("account_id")
        amt = txn.get("amount_float")
        amount_mag = _usable_magnitude(amt)
        day = _day_of_month(txn)
        assigned = False
        for rule in rules:
            if _match(rule, desc, payee, account_id, amount_mag, day):
                txn["category"] = rule["category"]
                txn["is_transfer"] = bool(rule["is_transfer"])
                txn["category_source"] = "rule"
                txn["is_income"] = is_income_category(
                    txn["category"], txn["is_transfer"]
                )
                assigned = True
                break
        if not assigned:
            txn["category"], txn["is_transfer"], txn["category_source"] = (
                UNCATEGORIZED, False, "none",
            )
            txn["is_income"] = False
    return transactions


def coverage_report(transactions: list[dict]) -> dict:
    """Tally coverage over transactions that have ALREADY been categorized.

    Use this with the list returned by ``store.load_archive_view`` (which applies
    categories on every read path, including the cache fallback) so the report
    matches what queries actually serve.
    """
    total = len(transactions)
    uncategorized = [
        t for t in transactions if t.get("category", UNCATEGORIZED) == UNCATEGORIZED
    ]
    by_cat: dict[str, int] = {}
    for t in transactions:
        cat = t.get("category", UNCATEGORIZED)
        by_cat[cat] = by_cat.get(cat, 0) + 1
    return {
        "total": total,
        "categorized": total - len(uncategorized),
        "uncategorized": len(uncategorized),
        "coverage_pct": round(100 * (total - len(uncategorized)) / total, 1) if total else 0.0,
        "categories": dict(sorted(by_cat.items(), key=lambda kv: -kv[1])),
    }


def coverage(conn: sqlite3.Connection, transactions: list[dict]) -> dict:
    """Apply categories to the given transactions, then tally coverage."""
    apply_categories(conn, transactions)
    return coverage_report(transactions)
