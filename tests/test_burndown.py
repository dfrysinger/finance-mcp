"""Tests for envelope burn-down: the pure function and an end-to-end report."""

import json

import pytest

from finance_mcp import archive, budget_config, burndown, categories


def _txn(tid, account, amount, *, date="2026-05-15", desc="", payee="",
         is_transfer=False, category="Groceries"):
    return {
        "id": tid,
        "account_id": account,
        "account_name": account,
        "amount": amount,
        "amount_float": float(amount),
        "posted": f"{date}T00:00:00+00:00",
        "description": desc,
        "payee": payee,
        "is_transfer": is_transfer,
        "category": category,
    }


def _config(*envelopes):
    return budget_config.parse_config({"version": 1, "envelopes": list(envelopes)})


GROCERIES = {"name": "Groceries", "accounts": ["g"], "monthly_target": 750.00}
DINING = {"name": "Dining", "accounts": ["d"], "monthly_target": 200.00}


def _env(report, name):
    return next(e for e in report["envelopes"] if e["envelope"] == name)


# --- Core spend math -----------------------------------------------------------


def test_basic_outflow_is_spend():
    cfg = _config(GROCERIES)
    txns = [_txn("1", "g", "-100.00"), _txn("2", "g", "-50.00")]
    r = burndown.burndown(txns, cfg, year=2026, month=5)
    g = _env(r, "Groceries")
    assert g["actual_spend"] == 150.00
    assert g["remaining"] == 600.00
    assert g["over_budget"] is False
    assert g["txn_count"] == 2


def test_inflow_credit_does_not_count_as_spend():
    cfg = _config(GROCERIES)
    txns = [_txn("1", "g", "-100.00"), _txn("2", "g", "200.00", category="Groceries")]
    g = _env(burndown.burndown(txns, cfg, year=2026, month=5), "Groceries")
    # Headline spend stays the outflow; the credit is surfaced as a refund only.
    assert g["actual_spend"] == 100.00
    assert g["refunds"] == 200.00
    assert g["net_spend"] == -100.00


def test_income_credit_is_not_a_refund():
    cfg = _config(GROCERIES)
    txns = [_txn("1", "g", "-100.00"), _txn("2", "g", "500.00", category="Income")]
    g = _env(burndown.burndown(txns, cfg, year=2026, month=5), "Groceries")
    assert g["actual_spend"] == 100.00
    assert g["refunds"] == 0.0


def test_over_budget_flag_and_remaining_negative():
    cfg = _config(DINING)
    txns = [_txn("1", "d", "-250.00", category="Dining")]
    d = _env(burndown.burndown(txns, cfg, year=2026, month=5), "Dining")
    assert d["over_budget"] is True
    assert d["remaining"] == -50.00
    assert d["pct_used"] == 125.0


def test_exactly_on_target_is_not_over():
    cfg = _config(DINING)
    txns = [_txn("1", "d", "-200.00", category="Dining")]
    d = _env(burndown.burndown(txns, cfg, year=2026, month=5), "Dining")
    assert d["over_budget"] is False
    assert d["remaining"] == 0.0


def test_float_drift_does_not_cause_false_overrun():
    # Three 250.00 outflows == 750.00 target exactly. Float summation of 2.50
    # repeated could drift; integer cents cannot.
    cfg = _config(GROCERIES)
    txns = [_txn(str(i), "g", "-250.00") for i in range(3)]
    g = _env(burndown.burndown(txns, cfg, year=2026, month=5), "Groceries")
    assert g["actual_spend"] == 750.00
    assert g["over_budget"] is False
    assert g["remaining"] == 0.0


# --- Transfer exclusion --------------------------------------------------------


def test_category_transfer_excluded_from_spend():
    cfg = _config(GROCERIES)
    txns = [
        _txn("1", "g", "-100.00"),
        _txn("2", "g", "-500.00", is_transfer=True, category="Transfer"),
    ]
    g = _env(burndown.burndown(txns, cfg, year=2026, month=5), "Groceries")
    assert g["actual_spend"] == 100.00
    assert g["excluded_by_category"] == 1


def test_reconciled_link_leg_excluded_from_spend():
    cfg = _config(GROCERIES)
    txns = [_txn("1", "g", "-100.00"), _txn("xfer", "g", "-500.00")]
    g = _env(
        burndown.burndown(
            txns, cfg, year=2026, month=5, reconciled_leg_ids=frozenset({"xfer"})
        ),
        "Groceries",
    )
    assert g["actual_spend"] == 100.00
    assert g["excluded_by_link"] == 1
    assert g["excluded_by_category"] == 0


def test_unmatched_transfer_leg_still_counts_as_spend():
    # Not in reconciled set and not category-flagged -> stays in spend.
    cfg = _config(GROCERIES)
    txns = [_txn("1", "g", "-500.00")]
    g = _env(
        burndown.burndown(txns, cfg, year=2026, month=5, reconciled_leg_ids=frozenset()),
        "Groceries",
    )
    assert g["actual_spend"] == 500.00


# --- Month boundaries ----------------------------------------------------------


def test_only_target_month_counted():
    cfg = _config(GROCERIES)
    txns = [
        _txn("apr", "g", "-100.00", date="2026-04-30"),
        _txn("may", "g", "-200.00", date="2026-05-01"),
        _txn("may2", "g", "-300.00", date="2026-05-31"),
        _txn("jun", "g", "-400.00", date="2026-06-01"),
    ]
    g = _env(burndown.burndown(txns, cfg, year=2026, month=5), "Groceries")
    assert g["actual_spend"] == 500.00


def test_undated_transaction_excluded_without_inflating_diagnostics():
    cfg = _config(GROCERIES)
    txns = [_txn("1", "g", "-100.00")]
    txns.append({"id": "x", "account_id": "g", "amount": "-9.00", "posted": None,
                 "is_transfer": False, "category": "Groceries"})
    r = burndown.burndown(txns, cfg, year=2026, month=5)
    # The undated row is excluded from spend (it cannot be placed in any month).
    assert _env(r, "Groceries")["actual_spend"] == 100.00
    # Like an out-of-month row, an undated row is not counted as a per-month skip,
    # so a multi-year archive's undated rows never inflate one month's report.
    assert "undated_skipped" not in r["diagnostics"]


def test_undated_rows_do_not_inflate_a_single_month_report():
    cfg = _config(GROCERIES)
    txns = [_txn("1", "g", "-100.00")]
    # Several undated rows that would otherwise accumulate archive-wide.
    for i in range(3):
        txns.append({"id": f"u{i}", "account_id": "g", "amount": "-1.00",
                     "posted": None, "is_transfer": False, "category": "Groceries"})
    r = burndown.burndown(txns, cfg, year=2026, month=5)
    assert _env(r, "Groceries")["actual_spend"] == 100.00
    assert r["diagnostics"] == {"amount_missing": 0}


# --- Unmapped + missing data ---------------------------------------------------


def test_unmapped_spend_surfaced():
    cfg = _config(GROCERIES)
    txns = [_txn("1", "g", "-100.00"), _txn("2", "other", "-75.00")]
    r = burndown.burndown(txns, cfg, year=2026, month=5)
    assert r["totals"]["total_unmapped_spend"] == 75.00
    assert r["unmapped"][0]["account_id"] == "other"
    assert r["unmapped"][0]["actual_spend"] == 75.00


def test_unmapped_transfer_not_surfaced_as_spend():
    cfg = _config(GROCERIES)
    txns = [_txn("2", "other", "-75.00", is_transfer=True, category="Transfer")]
    r = burndown.burndown(txns, cfg, year=2026, month=5)
    assert r["unmapped"] == []
    assert r["totals"]["total_unmapped_spend"] == 0.0


def test_amount_missing_counted():
    cfg = _config(GROCERIES)
    txns = [{
        "id": "x", "account_id": "g", "amount": None, "amount_float": None,
        "posted": "2026-05-10T00:00:00+00:00", "is_transfer": False, "category": "Groceries",
    }]
    r = burndown.burndown(txns, cfg, year=2026, month=5)
    assert r["diagnostics"]["amount_missing"] == 1
    assert _env(r, "Groceries")["actual_spend"] == 0.0


def test_envelope_with_no_txns_listed_at_zero():
    cfg = _config(GROCERIES, DINING)
    txns = [_txn("1", "g", "-100.00")]
    d = _env(burndown.burndown(txns, cfg, year=2026, month=5), "Dining")
    assert d["actual_spend"] == 0.0
    assert d["remaining"] == 200.00


# --- No-target envelopes -------------------------------------------------------


def test_no_target_envelope_has_null_status():
    cfg = _config({"name": "Hub", "accounts": ["h"], "role": "hub"})
    txns = [_txn("1", "h", "-100.00", category="Other")]
    h = _env(burndown.burndown(txns, cfg, year=2026, month=5), "Hub")
    assert h["monthly_target"] is None
    assert h["remaining"] is None
    assert h["over_budget"] is None
    assert h["pct_used"] is None
    assert h["actual_spend"] == 100.00


def test_zero_target_pct_is_none_but_over_when_spent():
    cfg = _config({"name": "Z", "accounts": ["z"], "monthly_target": 0})
    txns = [_txn("1", "z", "-10.00", category="Other")]
    z = _env(burndown.burndown(txns, cfg, year=2026, month=5), "Z")
    assert z["pct_used"] is None
    assert z["over_budget"] is True


# --- Totals --------------------------------------------------------------------


def test_totals_aggregate_across_envelopes():
    cfg = _config(GROCERIES, DINING)
    txns = [_txn("1", "g", "-800.00"), _txn("2", "d", "-50.00", category="Dining")]
    r = burndown.burndown(txns, cfg, year=2026, month=5)
    t = r["totals"]
    assert t["total_target"] == 950.00
    assert t["total_actual_spend"] == 850.00
    assert t["total_remaining"] == 100.00
    assert t["envelopes_over_budget"] == 1


def test_invalid_month_rejected():
    cfg = _config(GROCERIES)
    with pytest.raises(ValueError, match="month must be"):
        burndown.burndown([], cfg, year=2026, month=13)


def test_present_but_subcent_amount_is_data_quality_not_rounded():
    # An authoritative amount string that amount_to_cents rejects (sub-cent)
    # must be surfaced as amount_missing, never silently rounded into spend.
    cfg = _config(GROCERIES)
    txns = [{
        "id": "x", "account_id": "g", "amount": "-70.005", "amount_float": -70.005,
        "posted": "2026-05-10T00:00:00+00:00", "is_transfer": False, "category": "Groceries",
    }]
    r = burndown.burndown(txns, cfg, year=2026, month=5)
    assert _env(r, "Groceries")["actual_spend"] == 0.0
    assert r["diagnostics"]["amount_missing"] == 1


def test_legacy_float_only_subcent_rejected():
    # Legacy row with no authoritative string and a sub-cent float is rejected,
    # not rounded.
    cfg = _config(GROCERIES)
    txns = [{
        "id": "x", "account_id": "g", "amount": None, "amount_float": -70.005,
        "posted": "2026-05-10T00:00:00+00:00", "is_transfer": False, "category": "Groceries",
    }]
    r = burndown.burndown(txns, cfg, year=2026, month=5)
    assert _env(r, "Groceries")["actual_spend"] == 0.0
    assert r["diagnostics"]["amount_missing"] == 1


def test_legacy_float_only_whole_cent_counted():
    cfg = _config(GROCERIES)
    txns = [{
        "id": "x", "account_id": "g", "amount": None, "amount_float": -70.00,
        "posted": "2026-05-10T00:00:00+00:00", "is_transfer": False, "category": "Groceries",
    }]
    g = _env(burndown.burndown(txns, cfg, year=2026, month=5), "Groceries")
    assert g["actual_spend"] == 70.00


def test_no_target_envelope_spend_excluded_from_total_remaining():
    cfg = _config(GROCERIES, {"name": "Hub", "accounts": ["h"], "role": "hub"})
    txns = [
        _txn("g1", "g", "-100.00"),
        _txn("h1", "h", "-9000.00", category="Other"),  # huge, but unbudgeted
    ]
    t = burndown.burndown(txns, cfg, year=2026, month=5)["totals"]
    # The hub's $9000 spend must not deflate the budgeted remaining.
    assert t["total_target"] == 750.00
    assert t["total_actual_spend"] == 100.00
    assert t["total_remaining"] == 650.00
    assert t["total_untargeted_spend"] == 9000.00


# --- End-to-end through the archive + report wrapper ---------------------------


def test_end_to_end_report(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    # Live transactions are served from the JSON cache by load_archive_view; write
    # one so the read path has data, plus an in-archive transfer-link leg.
    conn = archive.connect()
    try:
        categories.seed_default_rules(conn)
        normalized = {
            "accounts": [],
            "transactions": [
                _txn("g1", "g", "-100.00", desc="HARMONS GROCERY"),
                _txn("g2", "g", "-700.00", desc="COSTCO WHSE"),
                _txn("d1", "d", "-50.00", desc="CHIPOTLE", category="Dining"),
                _txn("x1", "g", "-500.00", desc="ACH DEBIT 9981"),
            ],
        }
        archive.upsert(conn, normalized)
        # Reconcile one outflow as a transfer link leg (confirmed).
        archive.insert_transfer_link(
            conn, status="confirmed", debit_txn_id="x1", amount_cents=50000
        )
    finally:
        conn.close()

    cfg = budget_config.parse_config(
        {"version": 1, "envelopes": [GROCERIES, DINING]}
    )
    report = burndown.burndown_report(cfg, year=2026, month=5)

    g = _env(report, "Groceries")
    # 100 + 700 = 800 outflow; the 500 transfer leg is excluded.
    assert g["actual_spend"] == 800.00
    assert g["over_budget"] is True
    assert g["excluded_by_link"] == 1
    d = _env(report, "Dining")
    assert d["actual_spend"] == 50.00
    # Round-trips as JSON (CLI --json path).
    json.dumps(report)


def test_report_excludes_via_category_when_no_links(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    conn = archive.connect()
    try:
        categories.seed_default_rules(conn)
        archive.upsert(conn, {
            "accounts": [],
            "transactions": [
                _txn("g1", "g", "-120.00", desc="WALMART"),
                _txn("x1", "g", "-300.00", desc="Online Transfer to savings"),
            ],
        })
    finally:
        conn.close()
    cfg = budget_config.parse_config({"version": 1, "envelopes": [GROCERIES]})
    report = burndown.burndown_report(cfg, year=2026, month=5)
    g = _env(report, "Groceries")
    # No transfer_links rows exist, but the category rule flags the transfer.
    assert g["actual_spend"] == 120.00
    assert g["excluded_by_category"] == 1
