"""Idempotent transfer reconciliation — persist matcher proposals durably.

The matching engine (:mod:`finance_mcp.matching`) reconstructs the hidden
counterparty of each internal transfer but never touches the database. This
module is the persistence layer that runs it against the archive and writes the
results into ``transfer_links`` under an idempotency policy that protects the
user's own decisions:

* **Confirmed links are authoritative.** A link the user confirmed
  (``status='confirmed'``) is never recomputed. Its legs are removed from the
  matcher's input so the rest of the graph resolves *around* the known-good
  pairing instead of competing with it — confirming one transfer can only make
  the remaining matches more certain, never reopen the confirmed one.
* **Machine links are recomputed every run.** Inferred, needs-confirm, and
  unmatched links are deleted and rebuilt from a fresh match, tagged with the
  run id, so a sync that adds or removes transactions is always reflected. A leg
  that had no counterparty last run and now pairs is promoted automatically by
  the recompute.
* **A changed pairing is never silently swapped.** If a leg the matcher
  previously inferred a counterparty (and direction) for now resolves to a
  *different* counterparty — or the same two transactions with their
  debit/credit roles reversed — the new link is not trusted on its own: it is
  downgraded to needs-confirm so the user reviews the change. The downgrade is
  **sticky across stable re-runs**: once flagged, a pairing the matcher keeps
  proposing unchanged stays needs-confirm until the user confirms it, so a later
  reconcile cannot quietly re-promote an answer the tool already told the user to
  check. Stickiness is scoped honestly — it is carried by the persisted
  needs-confirm link itself. If a flagged leg's counterparty later *leaves the
  archive* (e.g. a pending row that vanished or re-keyed), the link reverts to an
  honest ``unmatched`` rather than the tool retaining a needs-confirm row that
  points at a transaction no longer present; if that pairing re-emerges as the
  unique forced match, it is then a fair promotion from ``unmatched``, not a
  silent swap of a confident link.

Re-running reconcile over unchanged data is a no-op on the meaningful columns:
the match is deterministic, promotions and downgrades only fire when the
underlying pairing actually moves, and a downgraded pairing stays put.
"""

from __future__ import annotations

import uuid

from . import archive, categories
from .matching import (
    CONF_UNCONFIRMED,
    STATUS_INFERRED,
    STATUS_UNCONFIRMED,
    STATUS_UNMATCHED,
    propose_transfer_links,
)

CONFIRMED = "confirmed"


def _row_from_proposal(proposal) -> dict:
    """Flatten a :class:`~finance_mcp.matching.TransferProposal` into a link row.

    ``reconcile_run_id`` is left ``None`` here and stamped by :func:`reconcile`
    so the pure planning step stays free of run-scoped state.
    """
    return {
        "debit_txn_id": proposal.debit_txn_id,
        "credit_txn_id": proposal.credit_txn_id,
        "amount_cents": proposal.amount_cents,
        "status": proposal.status,
        "method": proposal.method,
        "confidence": proposal.confidence,
        "date_rule": proposal.date_rule,
        "keyword": proposal.keyword,
        "type_source": proposal.type_source,
        "candidates_before": proposal.candidates_before,
        "candidates_after": proposal.candidates_after,
        "explanation": proposal.explanation,
        "reconcile_run_id": None,
    }


def plan_links(existing_links: list[dict], proposals: list) -> tuple[list[dict], dict]:
    """Turn fresh proposals into rows to persist, applying the stability guard.

    Pure: no database access, so the policy can be exercised directly. Compares
    each freshly proposed *inferred link* (a proposal carrying both legs) against
    the prior contents of ``transfer_links`` and decides whether to trust it,
    keep it flagged for review, or newly flag it:

    * A pairing already persisted as needs-confirm (a both-leg ``unconfirmed``
      row — only this module writes those) stays needs-confirm. This is what
      makes a downgrade sticky across stable re-runs: the tool will not
      re-promote a pairing it already asked the user to confirm until they
      actually confirm it.
    * Otherwise, if either leg's previously persisted *directed* counterparty
      (partner id **and** debit/credit role) differs from the new one, the link
      is downgraded to needs-confirm and counted. Comparing direction, not just
      the partner id, catches a flow reversal (the same two transactions with
      their debit/credit roles swapped), which silently changes "from where → to
      where".
    * Otherwise the link is trusted as inferred; if either leg was unmatched last
      run, the promotion is counted.

    Returns ``(rows, stats)`` where ``stats`` is ``{"downgraded", "promoted"}``.
    ``promoted`` counts previously-unmatched *legs* that are now linked.
    """
    prior_directed: dict[str, tuple[str, str]] = {}
    prior_unmatched: set[str] = set()
    prior_unconfirmed_pairs: set[frozenset] = set()
    for link in existing_links:
        status = link.get("status")
        debit, credit = link.get("debit_txn_id"), link.get("credit_txn_id")
        if debit is not None and credit is not None and status in (
            STATUS_INFERRED,
            STATUS_UNCONFIRMED,
        ):
            prior_directed[debit] = ("debit", credit)
            prior_directed[credit] = ("credit", debit)
            if status == STATUS_UNCONFIRMED:
                prior_unconfirmed_pairs.add(frozenset((debit, credit)))
        elif status == STATUS_UNMATCHED:
            for leg in (debit, credit):
                if leg is not None:
                    prior_unmatched.add(leg)

    flagged_explanation = (
        "Pairing changed from an earlier reconcile; needs confirmation before it "
        "is trusted."
    )

    rows: list[dict] = []
    downgraded = 0
    promoted = 0
    for proposal in proposals:
        row = _row_from_proposal(proposal)
        debit, credit = proposal.debit_txn_id, proposal.credit_txn_id
        is_link = (
            proposal.status == STATUS_INFERRED
            and debit is not None
            and credit is not None
        )
        if is_link:
            pair = frozenset((debit, credit))
            sticky = pair in prior_unconfirmed_pairs
            changed = (
                prior_directed.get(debit) not in (None, ("debit", credit))
                or prior_directed.get(credit) not in (None, ("credit", debit))
            )
            if sticky or changed:
                row["status"] = STATUS_UNCONFIRMED
                row["confidence"] = CONF_UNCONFIRMED
                row["method"] = None
                row["explanation"] = (
                    f"{flagged_explanation} {proposal.explanation or ''}".strip()
                )
                if changed and not sticky:
                    downgraded += 1
            else:
                promoted += sum(
                    1 for leg in (debit, credit) if leg in prior_unmatched
                )
        rows.append(row)
    return rows, {"downgraded": downgraded, "promoted": promoted}


def _report(run_id: str, rows: list[dict], stats: dict, confirmed_preserved: int) -> dict:
    """Tally written rows into a JSON-serializable reconcile report."""
    links = needs_confirm = unmatched = 0
    by_method: dict[str, int] = {}
    by_confidence: dict[str, int] = {}
    for row in rows:
        status = row["status"]
        if status == STATUS_INFERRED:
            links += 1
        elif status == STATUS_UNCONFIRMED:
            needs_confirm += 1
        elif status == STATUS_UNMATCHED:
            unmatched += 1
        method = row.get("method")
        if method:
            by_method[method] = by_method.get(method, 0) + 1
        confidence = row.get("confidence")
        if confidence:
            by_confidence[confidence] = by_confidence.get(confidence, 0) + 1
    return {
        "run_id": run_id,
        "total_written": len(rows),
        "links": links,
        "needs_confirm": needs_confirm,
        "unmatched": unmatched,
        "confirmed_preserved": confirmed_preserved,
        "promoted": stats["promoted"],
        "downgraded": stats["downgraded"],
        "by_method": by_method,
        "by_confidence": by_confidence,
    }


def reconcile(conn=None, *, run_id: str | None = None, is_transfer_key: str = "is_transfer") -> dict:
    """Run the matcher over the archive and persist links idempotently.

    Loads categorized transactions and the static product-type map from the
    archive, excludes legs already claimed by a confirmed link, runs the global
    matcher, applies the stability guard (:func:`plan_links`), and atomically
    replaces the machine-written links. ``conn`` defaults to the durable archive
    at ``home_dir()/archive.db``; when opened here it is closed again. A fresh
    ``run_id`` is generated per call unless supplied. Returns a report dict.
    """
    own_conn = conn is None
    if own_conn:
        conn = archive.connect()
    try:
        existing = archive.load_transfer_links(conn)
        confirmed_legs: set[str] = set()
        confirmed_preserved = 0
        for link in existing:
            if link.get("status") != CONFIRMED:
                continue
            confirmed_preserved += 1
            for leg in (link.get("debit_txn_id"), link.get("credit_txn_id")):
                if leg is not None:
                    confirmed_legs.add(leg)

        transactions = archive.load_transactions(conn)
        categories.apply_categories(conn, transactions)
        to_match = [t for t in transactions if t.get("id") not in confirmed_legs]
        account_types = archive.load_account_types(conn)

        proposals = propose_transfer_links(
            to_match, is_transfer_key=is_transfer_key, account_types=account_types
        )
        rows, stats = plan_links(existing, proposals)

        run_id = run_id or uuid.uuid4().hex
        for row in rows:
            row["reconcile_run_id"] = run_id

        archive.replace_machine_links(conn, rows)
        return _report(run_id, rows, stats, confirmed_preserved)
    finally:
        if own_conn:
            conn.close()
