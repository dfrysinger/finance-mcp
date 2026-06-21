"""Static account product-type map — cold-start seeding and confirmation.

An account's Schwab product type ("Investor Checking" vs "Investor Savings") is
permanent per account number, so it is stored once in ``account_types`` and used
by the matcher only as a tie-breaker / guard — never re-learned from the matches
it helps produce, which would be circular.

This module seeds suggestions for that table so the user is not asked to classify
~19 accounts from a blank slate:

* **Inferred (high trust).** A *structurally-certain* match (one that needed no
  type information to be drawn) reveals the counterparty account's type for free:
  a debit that says "...to Investor Checking" and is structurally forced to a
  particular credit proves that credit's account is Investor Checking. These
  carry source ``inferred``.
* **Heuristic (low trust).** An account whose *name* contains "checking"/"savings"
  gets that type as a fallback guess, source ``heuristic``.

A user ``confirmed`` type always wins and is never overwritten; an ``inferred``
suggestion is never downgraded to a ``heuristic`` one. The seed reads only
structural matches (computed WITHOUT the type map) so seeding can never feed on
its own output.
"""

from __future__ import annotations

import sqlite3

from . import archive
from .matching import CONF_STRUCTURAL, destination_type, propose_transfer_links

# Trust ordering: a higher rank is never overwritten by a lower one. ``confirmed``
# is user-authoritative; ``inferred`` comes from type-independent structural
# matches; ``heuristic`` is a name guess.
_RANK = {"confirmed": 3, "inferred": 2, "heuristic": 1}


def _inferred_types(
    transactions: list[dict],
    proposals: list,
) -> dict[str, set[str]]:
    """Gather the set of product types each account's structural matches imply.

    A debit's destination keyword types the credit account; a credit's source
    keyword types the debit account. Only type-independent structural matches
    (``CONF_STRUCTURAL``) are trusted, so the result can never feed on the type
    map's own output. An account mapping to >1 type has conflicting evidence.
    """
    keyword_by_txn = {
        t.get("id"): destination_type(t.get("description"))
        for t in transactions
        if t.get("id") is not None
    }
    inferred: dict[str, set[str]] = {}

    def _add(account_id: str | None, ptype: str | None) -> None:
        if account_id and ptype:
            inferred.setdefault(account_id, set()).add(ptype)

    for p in proposals:
        if p.confidence != CONF_STRUCTURAL:
            continue
        _add(p.credit_account_id, keyword_by_txn.get(p.debit_txn_id))
        _add(p.debit_account_id, keyword_by_txn.get(p.credit_txn_id))
    return inferred


def suggest_account_types(
    transactions: list[dict],
    proposals: list | None = None,
    *,
    is_transfer_key: str = "is_transfer",
) -> dict[str, tuple[str, str]]:
    """Compute suggested ``{account_id: (product_type, source)}`` (pure).

    ``proposals`` defaults to the structural-only matching of ``transactions``
    (no type map passed, so the result is type-independent and safe to learn
    from). Inferred suggestions from structural matches take precedence over
    name-hint heuristics. An account whose structural matches imply *conflicting*
    types is left unseeded rather than guessed.
    """
    if proposals is None:
        proposals = propose_transfer_links(transactions, is_transfer_key=is_transfer_key)

    inferred = _inferred_types(transactions, proposals)

    suggestions: dict[str, tuple[str, str]] = {}

    # Name-hint prior first (lowest trust); inferred overwrites it below.
    for t in transactions:
        aid = t.get("account_id")
        if not aid or aid in suggestions:
            continue
        hint = destination_type(t.get("account_name"))
        if hint:
            suggestions[aid] = (hint, "heuristic")

    # Inferred from structural matches. A single implied type wins over any name
    # hint; conflicting implied types are a positive signal something is off, so
    # the account is dropped entirely (even a name hint) and left for the user —
    # a wrong heuristic here would feed the matcher's guard and tie-breaker.
    for aid, types in inferred.items():
        if len(types) == 1:
            suggestions[aid] = (next(iter(types)), "inferred")
        else:
            suggestions.pop(aid, None)

    return suggestions


def seed_account_types(
    conn: sqlite3.Connection,
    transactions: list[dict],
    *,
    is_transfer_key: str = "is_transfer",
) -> dict:
    """Seed the ``account_types`` table from structural matches + name hints.

    Confirmed entries are never touched and an existing higher-trust source is
    never downgraded. An account whose structural evidence has turned
    *contradictory* has any prior non-confirmed guess cleared, so a stale
    heuristic the pure suggestion deliberately dropped cannot keep feeding the
    matcher's guard. Returns a report with the seeded accounts grouped by source,
    the count left untouched because a stronger entry already existed, and the
    count of stale guesses cleared.
    """
    proposals = propose_transfer_links(transactions, is_transfer_key=is_transfer_key)
    suggestions = suggest_account_types(
        transactions, proposals, is_transfer_key=is_transfer_key
    )
    inferred = _inferred_types(transactions, proposals)
    conflicted = {aid for aid, types in inferred.items() if len(types) > 1}
    existing = archive.load_account_types(conn)

    seeded: dict[str, list[str]] = {"inferred": [], "heuristic": []}
    preserved = 0
    for aid, (ptype, source) in suggestions.items():
        cur = existing.get(aid)
        if cur is not None:
            cur_source = cur.get("source") or "heuristic"
            if cur_source == "confirmed" or _RANK.get(source, 0) < _RANK.get(cur_source, 0):
                preserved += 1
                continue
        archive.set_account_type(conn, aid, ptype, source=source)
        seeded.setdefault(source, []).append(aid)

    # Clear a now-doubted lower-trust guess for an account whose evidence
    # conflicts; a user-confirmed type is authoritative and is left alone.
    cleared = 0
    for aid in conflicted:
        cur = existing.get(aid)
        if cur is not None and (cur.get("source") or "heuristic") != "confirmed":
            archive.delete_account_type(conn, aid)
            cleared += 1

    return {
        "seeded": sum(len(v) for v in seeded.values()),
        "by_source": {k: len(v) for k, v in seeded.items()},
        "preserved": preserved,
        "cleared": cleared,
        "accounts": seeded,
    }


def confirm_account_type(
    conn: sqlite3.Connection, account_id: str, product_type: str
) -> None:
    """Pin a user-authoritative (``confirmed``) product type for one account."""
    pt = (product_type or "").strip()
    if not pt:
        raise ValueError("product_type must not be empty")
    archive.set_account_type(conn, account_id, pt, source="confirmed")
