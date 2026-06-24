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


def add_rule(
    conn: sqlite3.Connection,
    pattern: str,
    category: str,
    *,
    field: str = "any",
    is_transfer: bool = False,
    priority: int = 100,
    account_id: str | None = None,
) -> int:
    """Add a single rule; returns its rule_id.

    ``account_id`` scopes the rule to one account: when set, the rule matches
    only transactions on that account (so an account-specific override can
    reclassify a generic descriptor — e.g. a loan payment that the bank labels
    only "FUNDS TRAN" — without touching the same descriptor elsewhere). When
    ``None`` the rule applies to every account, as before.
    """
    if field not in ("description", "payee", "any"):
        raise ValueError("field must be description, payee, or any")
    pat = (pattern or "").strip().lower()
    cat = (category or "").strip()
    if not pat:
        raise ValueError("pattern must not be empty")
    if not cat:
        raise ValueError("category must not be empty")
    acct = (account_id or "").strip() or None
    cur = conn.execute(
        "INSERT INTO category_rules (pattern, field, category, is_transfer, priority, account_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (pat, field, cat, int(is_transfer), priority, acct, _now()),
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
        r["_p"] = (r["pattern"] or "").lower()
    return rules


def _match(rule: dict, desc: str, payee: str, account_id: str | None) -> bool:
    acct = rule.get("account_id")
    if acct is not None and acct != account_id:
        return False
    pat = rule["_p"]
    if not pat:
        return False
    field = rule["field"]
    if field == "description":
        return pat in desc
    if field == "payee":
        return pat in payee
    return pat in desc or pat in payee


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
        desc = (txn.get("description") or "").lower()
        payee = (txn.get("payee") or "").lower()
        account_id = txn.get("account_id")
        assigned = False
        for rule in rules:
            if _match(rule, desc, payee, account_id):
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
