"""Tests for idempotent transfer reconciliation.

Two layers: the pure stability-guard planner (:func:`reconcile.plan_links`),
exercised with hand-built link rows and proposals, and the end-to-end
:func:`reconcile.reconcile` path over a real on-disk archive.
"""

import json

import pytest

from finance_mcp import archive, categories, reconcile
from finance_mcp.matching import (
    CONF_STRUCTURAL,
    CONF_UNCONFIRMED,
    METHOD_MUTUAL_UNIQUE,
    STATUS_INFERRED,
    STATUS_UNCONFIRMED,
    STATUS_UNMATCHED,
    TransferProposal,
)


# --- Helpers -------------------------------------------------------------------

def _link_proposal(debit, credit, *, amount=10000):
    """A freshly-inferred mutual-unique link proposal (both legs present)."""
    return TransferProposal(
        status=STATUS_INFERRED,
        confidence=CONF_STRUCTURAL,
        amount_cents=amount,
        debit_txn_id=debit,
        credit_txn_id=credit,
        method=METHOD_MUTUAL_UNIQUE,
        explanation="mutual-unique.",
    )


def _existing(debit, credit, status, *, confidence=CONF_STRUCTURAL):
    return {"debit_txn_id": debit, "credit_txn_id": credit, "status": status,
            "confidence": confidence}


def _txn(tid, account, amount, *, date="2026-05-15", desc=""):
    return {
        "id": tid, "account_id": account, "account_name": account,
        "amount": amount, "amount_float": float(amount),
        "posted": f"{date}T00:00:00+00:00", "description": desc, "payee": "",
    }


def _mark_transfers(conn, *txn_ids):
    for tid in txn_ids:
        categories.set_manual_category(conn, tid, "Transfer", is_transfer=True)


def _link_for(links, leg):
    return next(
        link for link in links
        if link["debit_txn_id"] == leg or link["credit_txn_id"] == leg
    )


# --- Pure planner: the stability guard -----------------------------------------

def test_fresh_inferred_link_with_no_history_is_trusted():
    rows, stats = reconcile.plan_links([], [_link_proposal("d", "c")])
    assert len(rows) == 1
    assert rows[0]["status"] == STATUS_INFERRED
    assert stats == {"downgraded": 0, "promoted": 0}


def test_stable_rerun_keeps_inferred_and_counts_nothing():
    existing = [_existing("d", "c", STATUS_INFERRED)]
    rows, stats = reconcile.plan_links(existing, [_link_proposal("d", "c")])
    assert rows[0]["status"] == STATUS_INFERRED
    assert stats == {"downgraded": 0, "promoted": 0}


def test_changed_counterparty_is_downgraded_not_silently_replaced():
    # Last run paired d->c1; the matcher now forces d->c2 instead.
    existing = [_existing("d", "c1", STATUS_INFERRED)]
    rows, stats = reconcile.plan_links(existing, [_link_proposal("d", "c2")])
    assert rows[0]["status"] == STATUS_UNCONFIRMED
    assert rows[0]["confidence"] == CONF_UNCONFIRMED
    assert rows[0]["method"] is None
    assert "needs confirmation" in rows[0]["explanation"]
    assert stats["downgraded"] == 1


def test_flow_direction_reversal_is_downgraded():
    # Same two transactions, debit/credit roles swapped: the reconstructed
    # "from -> to" flow reverses, so it must not stay silently inferred.
    existing = [_existing("a", "b", STATUS_INFERRED)]
    rows, stats = reconcile.plan_links(existing, [_link_proposal("b", "a")])
    assert rows[0]["status"] == STATUS_UNCONFIRMED
    assert stats["downgraded"] == 1


def test_downgrade_is_sticky_across_a_later_stable_run():
    # The pairing was downgraded last run (persisted as a both-leg unconfirmed
    # row). Even though the matcher now re-proposes it as a confident link, it
    # stays needs-confirm until the user acts — never silently re-promoted.
    existing = [_existing("d", "c2", STATUS_UNCONFIRMED, confidence=CONF_UNCONFIRMED)]
    rows, stats = reconcile.plan_links(existing, [_link_proposal("d", "c2")])
    assert rows[0]["status"] == STATUS_UNCONFIRMED
    assert "needs confirmation" in rows[0]["explanation"]
    # A sticky carry-over is not a *new* downgrade.
    assert stats["downgraded"] == 0


def test_sticky_explanation_matches_the_first_downgrade_text():
    # One-step idempotency on the audit prose: the text a downgrade writes and
    # the text the next stable (sticky) run writes are identical, so a
    # downgraded row reaches its fixpoint in a single run.
    proposal = _link_proposal("d", "c2")
    downgrade_rows, _ = reconcile.plan_links(
        [_existing("d", "c1", STATUS_INFERRED)], [proposal]
    )
    sticky_rows, _ = reconcile.plan_links(
        [_existing("d", "c2", STATUS_UNCONFIRMED, confidence=CONF_UNCONFIRMED)],
        [proposal],
    )
    assert downgrade_rows[0]["explanation"] == sticky_rows[0]["explanation"]


def test_sticky_pairing_that_changes_again_is_re_downgraded():
    existing = [_existing("d", "c2", STATUS_UNCONFIRMED, confidence=CONF_UNCONFIRMED)]
    rows, stats = reconcile.plan_links(existing, [_link_proposal("d", "c3")])
    assert rows[0]["status"] == STATUS_UNCONFIRMED
    assert "needs confirmation" in rows[0]["explanation"]
    assert stats["downgraded"] == 1


def test_previously_unmatched_leg_is_promoted():
    existing = [
        {"debit_txn_id": "d", "credit_txn_id": None, "status": STATUS_UNMATCHED},
        {"debit_txn_id": None, "credit_txn_id": "c", "status": STATUS_UNMATCHED},
    ]
    rows, stats = reconcile.plan_links(existing, [_link_proposal("d", "c")])
    assert rows[0]["status"] == STATUS_INFERRED
    # Both legs were unmatched last run; both are now linked.
    assert stats["promoted"] == 2
    assert stats["downgraded"] == 0


def test_needs_confirm_single_leg_proposal_passes_through():
    proposal = TransferProposal(
        status=STATUS_UNCONFIRMED, confidence=CONF_UNCONFIRMED, amount_cents=5000,
        debit_txn_id="d", credit_txn_id=None, candidate_txn_ids=("c1", "c2"),
        explanation="two candidates.",
    )
    rows, stats = reconcile.plan_links([], [proposal])
    assert rows[0]["status"] == STATUS_UNCONFIRMED
    assert stats == {"downgraded": 0, "promoted": 0}


# --- End-to-end over a real archive --------------------------------------------

def test_reconcile_empty_archive_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    report = reconcile.reconcile()
    assert report["total_written"] == 0
    assert report["links"] == 0
    assert report["confirmed_preserved"] == 0
    json.dumps(report)


def test_reconcile_writes_a_mutual_unique_link(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    conn = archive.connect()
    try:
        archive.upsert(conn, {"accounts": [], "transactions": [
            _txn("d1", "Groceries", "-100.00", desc="Transfer to Main"),
            _txn("c1", "Main", "100.00", desc="Transfer from Groceries"),
        ]})
        _mark_transfers(conn, "d1", "c1")
    finally:
        conn.close()

    report = reconcile.reconcile()
    assert report["links"] == 1
    assert report["needs_confirm"] == 0

    conn = archive.connect()
    try:
        links = [link for link in archive.load_transfer_links(conn)
                 if link["status"] == STATUS_INFERRED]
        assert len(links) == 1
        assert links[0]["debit_txn_id"] == "d1"
        assert links[0]["credit_txn_id"] == "c1"
    finally:
        conn.close()


def test_reconcile_is_idempotent_on_meaningful_columns(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    conn = archive.connect()
    try:
        archive.upsert(conn, {"accounts": [], "transactions": [
            _txn("d1", "Groceries", "-100.00", desc="Transfer to Main"),
            _txn("c1", "Main", "100.00", desc="Transfer from Groceries"),
        ]})
        _mark_transfers(conn, "d1", "c1")
    finally:
        conn.close()

    def _meaningful():
        conn = archive.connect()
        try:
            return [
                {k: link[k] for k in (
                    "debit_txn_id", "credit_txn_id", "amount_cents", "status",
                    "method", "confidence", "explanation",
                )}
                for link in archive.load_transfer_links(conn)
            ]
        finally:
            conn.close()

    first_report = reconcile.reconcile()
    first = _meaningful()
    second_report = reconcile.reconcile()
    second = _meaningful()

    assert first == second
    # The run id changes every pass; the links it points at do not.
    assert first_report["run_id"] != second_report["run_id"]
    assert first_report["links"] == second_report["links"] == 1
    assert second_report["downgraded"] == 0
    assert second_report["promoted"] == 0


def test_reconcile_preserves_confirmed_link_and_excludes_its_legs(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    conn = archive.connect()
    try:
        # The user confirmed d1->c1. A second same-amount credit (c2) exists that
        # the matcher would otherwise weigh against c1; confirmation must take c1
        # off the table so c2 is left as the only candidate for d2.
        archive.upsert(conn, {"accounts": [], "transactions": [
            _txn("d1", "Groceries", "-100.00", desc="Transfer to Main"),
            _txn("c1", "Main", "100.00", desc="Transfer from Groceries"),
            _txn("d2", "Dining", "-100.00", desc="Transfer to Main"),
            _txn("c2", "Main", "100.00", desc="Transfer from Dining"),
        ]})
        _mark_transfers(conn, "d1", "c1", "d2", "c2")
        archive.insert_transfer_link(
            conn, status="confirmed", debit_txn_id="d1", credit_txn_id="c1",
            amount_cents=10000,
        )
    finally:
        conn.close()

    report = reconcile.reconcile()
    assert report["confirmed_preserved"] == 1

    conn = archive.connect()
    try:
        links = archive.load_transfer_links(conn)
    finally:
        conn.close()

    confirmed = _link_for(links, "d1")
    assert confirmed["status"] == "confirmed"
    # d2 now pairs cleanly with the only remaining credit, c2.
    inferred = _link_for(links, "d2")
    assert inferred["status"] == STATUS_INFERRED
    assert inferred["credit_txn_id"] == "c2"


def test_reconcile_promotes_a_previously_unmatched_leg(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    # First sync: only the debit landed, so it has no counterparty.
    conn = archive.connect()
    try:
        archive.upsert(conn, {"accounts": [], "transactions": [
            _txn("d1", "Groceries", "-100.00", desc="Transfer to Main"),
        ]})
        _mark_transfers(conn, "d1")
    finally:
        conn.close()
    first = reconcile.reconcile()
    assert first["links"] == 0
    assert first["unmatched"] == 1

    # Next sync brings in the matching credit.
    conn = archive.connect()
    try:
        archive.upsert(conn, {"accounts": [], "transactions": [
            _txn("c1", "Main", "100.00", desc="Transfer from Groceries"),
        ]})
        _mark_transfers(conn, "c1")
    finally:
        conn.close()
    second = reconcile.reconcile()
    assert second["links"] == 1
    assert second["unmatched"] == 0
    assert second["promoted"] == 1


def test_reconcile_downgrades_when_a_paired_leg_repairs_to_a_new_partner(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    # Run 1: d1 pairs with c1 (mutual-unique).
    conn = archive.connect()
    try:
        archive.upsert(conn, {"accounts": [], "transactions": [
            _txn("d1", "Groceries", "-100.00", desc="Transfer to Main"),
            _txn("c1", "Main", "100.00", desc="Transfer from Groceries"),
        ]})
        _mark_transfers(conn, "d1", "c1")
    finally:
        conn.close()
    assert reconcile.reconcile()["links"] == 1

    # The original credit disappears from the feed (e.g. a pending row that
    # re-keyed) and a different same-amount credit takes its place. The matcher
    # now forces d1->c2 — a different answer than last run.
    conn = archive.connect()
    try:
        conn.execute("DELETE FROM transactions WHERE id='c1'")
        conn.commit()
        archive.upsert(conn, {"accounts": [], "transactions": [
            _txn("c2", "Main", "100.00", desc="Transfer from Groceries"),
        ]})
        _mark_transfers(conn, "c2")
    finally:
        conn.close()

    report = reconcile.reconcile()
    assert report["downgraded"] == 1
    assert report["links"] == 0
    assert report["needs_confirm"] == 1

    conn = archive.connect()
    try:
        link = _link_for(archive.load_transfer_links(conn), "d1")
        assert link["status"] == STATUS_UNCONFIRMED
        assert link["credit_txn_id"] == "c2"
        downgrade_explanation = link["explanation"]
        # A downgraded link is not reconciled, so its legs are not silently
        # excluded from spend until the user confirms the new pairing.
        from finance_mcp.burndown import reconciled_leg_ids
        assert "d1" not in reconciled_leg_ids(conn)
    finally:
        conn.close()

    # A further stable run leaves the downgrade in place (sticky) and is a no-op
    # on the meaningful columns — including the audit explanation.
    sticky = reconcile.reconcile()
    assert sticky["needs_confirm"] == 1
    assert sticky["links"] == 0
    assert sticky["downgraded"] == 0
    conn = archive.connect()
    try:
        link = _link_for(archive.load_transfer_links(conn), "d1")
        assert link["explanation"] == downgrade_explanation
    finally:
        conn.close()


def test_pair_that_vanishes_then_returns_is_promoted_not_kept_dangling(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    # d1 pairs with c1, then re-keys to c2 (a flagged downgrade), then c2 itself
    # leaves the feed for a sync. The tool must not keep a needs-confirm link
    # pointing at a transaction no longer in the archive: it reverts to an honest
    # unmatched. When c2 returns as the unique match, that is a fair promotion
    # from unmatched, not a silent swap of a confident link.
    def _sync(txns):
        conn = archive.connect()
        try:
            archive.upsert(conn, {"accounts": [], "transactions": txns})
            _mark_transfers(conn, *[t["id"] for t in txns])
        finally:
            conn.close()

    _sync([
        _txn("d1", "Groceries", "-100.00", desc="Transfer to Main"),
        _txn("c1", "Main", "100.00", desc="Transfer from Groceries"),
    ])
    reconcile.reconcile()

    # c1 re-keys to c2 -> d1->c2 is downgraded to needs-confirm.
    conn = archive.connect()
    try:
        conn.execute("DELETE FROM transactions WHERE id='c1'")
        conn.commit()
    finally:
        conn.close()
    _sync([_txn("c2", "Main", "100.00", desc="Transfer from Groceries")])
    assert reconcile.reconcile()["downgraded"] == 1

    # c2 leaves the feed -> d1 has no counterparty -> honest unmatched, no
    # dangling needs-confirm row.
    conn = archive.connect()
    try:
        conn.execute("DELETE FROM transactions WHERE id='c2'")
        conn.commit()
    finally:
        conn.close()
    gone = reconcile.reconcile()
    assert gone["needs_confirm"] == 0
    assert gone["unmatched"] == 1

    # c2 returns as the unique forced match -> promotion, not a re-flag.
    _sync([_txn("c2", "Main", "100.00", desc="Transfer from Groceries")])
    back = reconcile.reconcile()
    assert back["links"] == 1
    assert back["needs_confirm"] == 0
    assert back["promoted"] == 1
