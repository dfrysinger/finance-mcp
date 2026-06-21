"""Tests for the allocation audit: the pure matcher and an end-to-end report."""

from datetime import date

import pytest

from finance_mcp import allocation, archive, budget_config, categories, reconcile

# --- fixtures / helpers -------------------------------------------------------

MAY_START = date(2026, 5, 1)
MAY_END = date(2026, 5, 31)

HUB = {"name": "Paycheck", "accounts": ["hub"]}
GROCERIES = {"name": "Groceries", "accounts": ["g"], "monthly_target": 750.00}
SAVINGS = {"name": "Savings", "accounts": ["sav"]}


def _txn(tid, account, amount, *, on="2026-05-01", is_transfer=False):
    return {
        "id": tid,
        "account_id": account,
        "account_name": account,
        "amount": amount,
        "amount_float": float(amount),
        "posted": f"{on}T00:00:00+00:00",
        "description": "",
        "payee": "",
        "is_transfer": is_transfer,
    }


def _link(debit, credit, amount_cents, *, status="inferred"):
    return {
        "debit_txn_id": debit,
        "credit_txn_id": credit,
        "amount_cents": amount_cents,
        "status": status,
    }


def _config(envelopes, transfers):
    return budget_config.parse_config(
        {"version": 1, "envelopes": list(envelopes), "scheduled_transfers": list(transfers)}
    )


def _internal_transfer(amount=500.00, day=1):
    return {
        "name": "Fund groceries",
        "from": "Paycheck",
        "to": "Groceries",
        "amount": amount,
        "cadence": "monthly",
        "day": day,
    }


def _external_transfer(amount=2000.00, day=1):
    return {
        "name": "Direct deposit",
        "to": "Paycheck",
        "amount": amount,
        "cadence": "monthly",
        "day": day,
    }


def _only_occ(report, transfer_name):
    block = next(t for t in report["transfers"] if t["name"] == transfer_name)
    assert len(block["occurrences"]) == 1
    return block["occurrences"][0]


# --- internal (reconciled-link-backed) allocations ----------------------------


def test_internal_on_time():
    cfg = _config([HUB, GROCERIES], [_internal_transfer()])
    txns = [_txn("d", "hub", "-500.00"), _txn("c", "g", "500.00")]
    links = [_link("d", "c", 50000)]
    report = allocation.allocation_audit(cfg, txns, links, start=MAY_START, end=MAY_END)
    occ = _only_occ(report, "Fund groceries")
    assert occ["status"] == "on_time"
    assert occ["drift_days"] == 0
    assert occ["actual_amount"] == "500.00"
    assert occ["kind"] == "internal"
    assert occ["evidence_ids"] == ["d", "c"]


def test_internal_late_uses_credit_landing_date():
    cfg = _config([HUB, GROCERIES], [_internal_transfer()])
    # Debit posts on the 1st, the destination credit lands on the 4th.
    txns = [_txn("d", "hub", "-500.00", on="2026-05-01"),
            _txn("c", "g", "500.00", on="2026-05-04")]
    links = [_link("d", "c", 50000)]
    occ = _only_occ(
        allocation.allocation_audit(cfg, txns, links, start=MAY_START, end=MAY_END),
        "Fund groceries",
    )
    assert occ["status"] == "late"
    assert occ["drift_days"] == 3


def test_internal_early():
    cfg = _config([HUB, GROCERIES], [_internal_transfer()])
    txns = [_txn("d", "hub", "-500.00", on="2026-04-29"),
            _txn("c", "g", "500.00", on="2026-04-29")]
    links = [_link("d", "c", 50000)]
    occ = _only_occ(
        allocation.allocation_audit(cfg, txns, links, start=MAY_START, end=MAY_END),
        "Fund groceries",
    )
    assert occ["status"] == "early"
    assert occ["drift_days"] == -2


def test_internal_wrong_amount():
    cfg = _config([HUB, GROCERIES], [_internal_transfer(amount=500.00)])
    txns = [_txn("d", "hub", "-450.00"), _txn("c", "g", "450.00")]
    links = [_link("d", "c", 45000)]
    occ = _only_occ(
        allocation.allocation_audit(cfg, txns, links, start=MAY_START, end=MAY_END),
        "Fund groceries",
    )
    assert occ["status"] == "wrong_amount"
    assert occ["drift_days"] == 0
    assert occ["actual_amount"] == "450.00"
    assert occ["expected_amount"] == "500.00"


def test_internal_missing_when_no_link():
    cfg = _config([HUB, GROCERIES], [_internal_transfer()])
    txns = [_txn("d", "hub", "-500.00"), _txn("c", "g", "500.00")]
    occ = _only_occ(
        allocation.allocation_audit(cfg, txns, [], start=MAY_START, end=MAY_END),
        "Fund groceries",
    )
    assert occ["status"] == "missing"
    assert occ["actual_date"] is None
    assert occ["drift_days"] is None
    assert occ["evidence_ids"] == []


def test_needs_confirm_link_not_counted_as_fired():
    # An unconfirmed (needs-confirm) link is ambiguous; do not silently credit it.
    cfg = _config([HUB, GROCERIES], [_internal_transfer()])
    txns = [_txn("d", "hub", "-500.00"), _txn("c", "g", "500.00")]
    links = [_link("d", "c", 50000, status="unconfirmed")]
    occ = _only_occ(
        allocation.allocation_audit(cfg, txns, links, start=MAY_START, end=MAY_END),
        "Fund groceries",
    )
    assert occ["status"] == "missing"


def test_confirmed_link_counts():
    cfg = _config([HUB, GROCERIES], [_internal_transfer()])
    txns = [_txn("d", "hub", "-500.00"), _txn("c", "g", "500.00")]
    links = [_link("d", "c", 50000, status="confirmed")]
    occ = _only_occ(
        allocation.allocation_audit(cfg, txns, links, start=MAY_START, end=MAY_END),
        "Fund groceries",
    )
    assert occ["status"] == "on_time"


def test_internal_wrong_envelope_does_not_match():
    # Money moved Paycheck -> Savings, but the schedule funds Groceries.
    cfg = _config([HUB, GROCERIES, SAVINGS], [_internal_transfer()])
    txns = [_txn("d", "hub", "-500.00"), _txn("c", "sav", "500.00")]
    links = [_link("d", "c", 50000)]
    occ = _only_occ(
        allocation.allocation_audit(cfg, txns, links, start=MAY_START, end=MAY_END),
        "Fund groceries",
    )
    assert occ["status"] == "missing"


def test_internal_outside_tolerance_is_missing():
    cfg = _config([HUB, GROCERIES], [_internal_transfer()])
    txns = [_txn("d", "hub", "-500.00", on="2026-05-20"),
            _txn("c", "g", "500.00", on="2026-05-20")]
    links = [_link("d", "c", 50000)]
    occ = _only_occ(
        allocation.allocation_audit(
            cfg, txns, links, start=MAY_START, end=MAY_END, day_tolerance=7
        ),
        "Fund groceries",
    )
    assert occ["status"] == "missing"


def test_exact_amount_preferred_over_wrong_amount():
    cfg = _config([HUB, GROCERIES], [_internal_transfer(amount=500.00)])
    txns = [
        _txn("d1", "hub", "-450.00", on="2026-05-01"),
        _txn("c1", "g", "450.00", on="2026-05-01"),
        _txn("d2", "hub", "-500.00", on="2026-05-02"),
        _txn("c2", "g", "500.00", on="2026-05-02"),
    ]
    links = [_link("d1", "c1", 45000), _link("d2", "c2", 50000)]
    occ = _only_occ(
        allocation.allocation_audit(cfg, txns, links, start=MAY_START, end=MAY_END),
        "Fund groceries",
    )
    # The exact $500 (1 day off) beats the $450 same-day mismatch.
    assert occ["status"] == "late"
    assert occ["actual_amount"] == "500.00"
    assert occ["evidence_ids"] == ["d2", "c2"]


# --- external (direct-deposit) allocations ------------------------------------


def test_external_on_time():
    cfg = _config([HUB, GROCERIES], [_external_transfer(amount=2000.00)])
    txns = [_txn("p", "hub", "2000.00")]
    occ = _only_occ(
        allocation.allocation_audit(cfg, txns, [], start=MAY_START, end=MAY_END),
        "Direct deposit",
    )
    assert occ["status"] == "on_time"
    assert occ["kind"] == "external"
    assert occ["evidence_ids"] == ["p"]


def test_external_ignores_transfer_flagged_credit():
    cfg = _config([HUB, GROCERIES], [_external_transfer()])
    txns = [_txn("p", "hub", "2000.00", is_transfer=True)]
    occ = _only_occ(
        allocation.allocation_audit(cfg, txns, [], start=MAY_START, end=MAY_END),
        "Direct deposit",
    )
    assert occ["status"] == "missing"


def test_external_ignores_credit_that_is_a_link_leg():
    cfg = _config([HUB, GROCERIES], [_external_transfer()])
    txns = [_txn("p", "hub", "2000.00")]
    links = [_link(None, "p", 200000)]  # "p" is a transfer leg, not a deposit
    occ = _only_occ(
        allocation.allocation_audit(cfg, txns, links, start=MAY_START, end=MAY_END),
        "Direct deposit",
    )
    assert occ["status"] == "missing"


def test_external_ignores_outflow():
    cfg = _config([HUB, GROCERIES], [_external_transfer()])
    txns = [_txn("p", "hub", "-2000.00")]  # a debit cannot satisfy an inflow
    occ = _only_occ(
        allocation.allocation_audit(cfg, txns, [], start=MAY_START, end=MAY_END),
        "Direct deposit",
    )
    assert occ["status"] == "missing"


# --- consumption / windows ----------------------------------------------------


def test_one_actual_is_not_double_counted_across_months():
    cfg = _config([HUB, GROCERIES], [_internal_transfer(day=1)])
    txns = [_txn("d", "hub", "-500.00", on="2026-05-01"),
            _txn("c", "g", "500.00", on="2026-05-01")]
    links = [_link("d", "c", 50000)]
    report = allocation.allocation_audit(
        cfg, txns, links, start=date(2026, 5, 1), end=date(2026, 6, 30)
    )
    block = next(t for t in report["transfers"] if t["name"] == "Fund groceries")
    by_date = {o["expected_date"]: o["status"] for o in block["occurrences"]}
    assert by_date == {"2026-05-01": "on_time", "2026-06-01": "missing"}


def test_summary_counts_every_occurrence():
    cfg = _config([HUB, GROCERIES], [_internal_transfer(day=1)])
    txns = [_txn("d", "hub", "-500.00", on="2026-05-01"),
            _txn("c", "g", "500.00", on="2026-05-01")]
    links = [_link("d", "c", 50000)]
    report = allocation.allocation_audit(
        cfg, txns, links, start=date(2026, 5, 1), end=date(2026, 6, 30)
    )
    assert report["summary"]["on_time"] == 1
    assert report["summary"]["missing"] == 1


def test_internal_dates_strictly_by_credit_leg():
    # A reconciled link whose credit leg has no usable date cannot be judged for
    # drift off the source debit leg; it surfaces as missing instead.
    cfg = _config([HUB, GROCERIES], [_internal_transfer()])
    debit = _txn("d", "hub", "-500.00", on="2026-05-01")
    credit = _txn("c", "g", "500.00", on="2026-05-01")
    credit["posted"] = None
    links = [_link("d", "c", 50000)]
    occ = _only_occ(
        allocation.allocation_audit(
            cfg, [debit, credit], links, start=MAY_START, end=MAY_END
        ),
        "Fund groceries",
    )
    assert occ["status"] == "missing"


# --- input validation ---------------------------------------------------------


def test_reversed_window_raises():
    cfg = _config([HUB, GROCERIES], [_internal_transfer()])
    with pytest.raises(ValueError):
        allocation.allocation_audit(
            cfg, [], [], start=date(2026, 5, 31), end=date(2026, 5, 1)
        )


def test_negative_tolerance_raises():
    cfg = _config([HUB, GROCERIES], [_internal_transfer()])
    with pytest.raises(ValueError):
        allocation.allocation_audit(
            cfg, [], [], start=MAY_START, end=MAY_END, day_tolerance=-1
        )


# --- end-to-end over a real archive ------------------------------------------


def test_e2e_report_over_archive(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    conn = archive.connect()
    try:
        archive.upsert(
            conn,
            {
                "accounts": [],
                "transactions": [
                    _txn("d", "hub", "-500.00", on="2026-05-01"),
                    _txn("c", "g", "500.00", on="2026-05-01"),
                ],
            },
        )
        categories.set_manual_category(conn, "d", "Transfer", is_transfer=True)
        categories.set_manual_category(conn, "c", "Transfer", is_transfer=True)
        # Let the reconciler infer and persist the link.
        reconcile.reconcile(conn=conn)
    finally:
        conn.close()

    cfg = _config([HUB, GROCERIES], [_internal_transfer()])
    report = allocation.allocation_report(
        cfg, start=date(2026, 5, 1), end=date(2026, 5, 31)
    )
    occ = _only_occ(report, "Fund groceries")
    assert occ["status"] == "on_time"
    assert occ["kind"] == "internal"


def test_e2e_report_no_archive_yields_all_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    cfg = _config([HUB, GROCERIES], [_internal_transfer()])
    report = allocation.allocation_report(
        cfg, start=date(2026, 5, 1), end=date(2026, 5, 31)
    )
    occ = _only_occ(report, "Fund groceries")
    assert occ["status"] == "missing"
