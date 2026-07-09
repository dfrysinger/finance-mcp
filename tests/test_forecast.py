"""Tests for envelope sufficiency / forecast."""

from datetime import date, timedelta

import pytest

from finance_mcp import archive, budget_config, categories, forecast


def _cfg(envelopes, recurring=None, scheduled_transfers=None):
    data = {"version": 1, "envelopes": envelopes}
    if recurring is not None:
        data["recurring"] = recurring
    if scheduled_transfers is not None:
        data["scheduled_transfers"] = scheduled_transfers
    return budget_config.parse_config(data)


def _env(report, name):
    return next(e for e in report["envelopes"] if e["envelope"] == name)


GROC = {"name": "Groceries", "accounts": ["g"]}
HUB = {"name": "Hub", "accounts": ["h"]}


# --- _monthly_dates -----------------------------------------------------------


def test_monthly_dates_within_window():
    dates = forecast._monthly_dates(10, date(2026, 3, 1), date(2026, 4, 30))
    assert dates == [date(2026, 3, 10), date(2026, 4, 10)]


def test_monthly_dates_clamps_to_short_month():
    # Day 31 has no slot in February; it clamps to the last day, not an error.
    assert forecast._monthly_dates(31, date(2026, 2, 1), date(2026, 2, 28)) == [
        date(2026, 2, 28)
    ]


def test_monthly_dates_leap_year():
    assert forecast._monthly_dates(31, date(2024, 2, 1), date(2024, 2, 29)) == [
        date(2024, 2, 29)
    ]


def test_monthly_dates_window_is_closed_on_both_ends():
    # A due date landing exactly on `through` is included.
    assert forecast._monthly_dates(15, date(2026, 3, 1), date(2026, 3, 15)) == [
        date(2026, 3, 15)
    ]
    # A due date before as_of is excluded.
    assert forecast._monthly_dates(5, date(2026, 3, 10), date(2026, 3, 31)) == []


# --- forecast(): core verdicts ------------------------------------------------


def test_default_through_shifts_by_horizon():
    assert forecast.default_through(date(2026, 3, 1)) == date(2026, 3, 1) + timedelta(
        days=forecast.DEFAULT_HORIZON_DAYS
    )


def test_default_through_clamps_at_date_max():
    # A near-date.max as_of would overflow raw date arithmetic; the helper must
    # clamp to the representable edge instead of raising OverflowError.
    assert forecast.default_through(date.max) == date.max
    assert forecast.default_through(date(9999, 12, 1)) == date.max


def test_sufficient_when_balance_covers_bills():
    cfg = _cfg([GROC], recurring=[
        {"name": "Rent", "envelope": "Groceries", "amount": 100,
         "cadence": "monthly", "day": 15},
    ])
    r = forecast.forecast(cfg, {"g": 50000}, as_of=date(2026, 3, 1), through=date(2026, 3, 31))
    g = _env(r, "Groceries")
    assert g["verdict"] == "sufficient"
    assert g["current_balance"] == 500.00
    assert g["total_out"] == 100.00
    assert g["projected_end_balance"] == 400.00
    assert g["at_risk_date"] is None
    assert g["shortfall"] == 0.0
    assert r["summary"]["sufficient"] == 1


def test_at_risk_when_bills_exceed_balance():
    cfg = _cfg([GROC], recurring=[
        {"name": "Rent", "envelope": "Groceries", "amount": 700,
         "cadence": "monthly", "day": 10},
    ])
    r = forecast.forecast(cfg, {"g": 50000}, as_of=date(2026, 3, 1), through=date(2026, 3, 31))
    g = _env(r, "Groceries")
    assert g["verdict"] == "at_risk"
    assert g["at_risk_date"] == "2026-03-10"
    assert g["shortfall"] == 200.00
    assert g["projected_min_balance"] == -200.00
    assert r["summary"]["at_risk"] == 1


def test_external_inflow_credits_envelope():
    cfg = _cfg([GROC],
        recurring=[{"name": "Rent", "envelope": "Groceries", "amount": 600,
                    "cadence": "monthly", "day": 20}],
        scheduled_transfers=[{"name": "Deposit", "to": "Groceries", "amount": 600,
                              "cadence": "monthly", "day": 1}])
    r = forecast.forecast(cfg, {"g": 10000}, as_of=date(2026, 3, 1), through=date(2026, 3, 31))
    g = _env(r, "Groceries")
    assert g["total_in"] == 600.00
    assert g["total_out"] == 600.00
    assert g["verdict"] == "sufficient"  # 100 + 600 - 600 = 100 end, never < 0


def test_internal_transfer_conserves_money():
    # Hub funds Groceries; the hub must be debited, not just Groceries credited.
    cfg = _cfg([GROC, HUB],
        recurring=[{"name": "Food", "envelope": "Groceries", "amount": 100,
                    "cadence": "monthly", "day": 5}],
        scheduled_transfers=[{"name": "Fanout", "from": "Hub", "to": "Groceries",
                              "amount": 200, "cadence": "monthly", "day": 1}])
    r = forecast.forecast(cfg, {"g": 0, "h": 15000},
                          as_of=date(2026, 3, 1), through=date(2026, 3, 31))
    groc = _env(r, "Groceries")
    hub = _env(r, "Hub")
    assert groc["total_in"] == 200.00 and groc["verdict"] == "sufficient"
    # Hub at 150 minus a 200 fan-out goes to -50: the money was not fabricated.
    assert hub["total_out"] == 200.00
    assert hub["verdict"] == "at_risk"
    assert hub["at_risk_date"] == "2026-03-01"
    assert hub["shortfall"] == 50.00


def test_verdict_uses_minimum_not_end_balance():
    # Dips negative mid-window, then a later inflow recovers it. End is positive,
    # but the dip is a real overdraft -> at_risk.
    cfg = _cfg([GROC],
        recurring=[{"name": "Bill", "envelope": "Groceries", "amount": 100,
                    "cadence": "monthly", "day": 10}],
        scheduled_transfers=[{"name": "Late pay", "to": "Groceries", "amount": 200,
                              "cadence": "monthly", "day": 20}])
    r = forecast.forecast(cfg, {"g": 5000}, as_of=date(2026, 3, 1), through=date(2026, 3, 31))
    g = _env(r, "Groceries")
    assert g["projected_end_balance"] == 150.00
    assert g["projected_min_balance"] == -50.00
    assert g["verdict"] == "at_risk"
    assert g["at_risk_date"] == "2026-03-10"


def test_same_day_funding_dependent_flag():
    # Bill and its funding inflow land the same day; realistic ordering stays >= 0
    # but the inflow is load-bearing, so the dependence is flagged.
    cfg = _cfg([GROC],
        recurring=[{"name": "Bill", "envelope": "Groceries", "amount": 100,
                    "cadence": "monthly", "day": 15}],
        scheduled_transfers=[{"name": "Pay", "to": "Groceries", "amount": 100,
                              "cadence": "monthly", "day": 15}])
    r = forecast.forecast(cfg, {"g": 0}, as_of=date(2026, 3, 1), through=date(2026, 3, 31))
    g = _env(r, "Groceries")
    assert g["verdict"] == "sufficient"
    assert g["projected_min_balance"] == 0.0
    assert g["same_day_funding_dependent"] is True


def test_no_same_day_flag_when_cushion_exists():
    cfg = _cfg([GROC],
        recurring=[{"name": "Bill", "envelope": "Groceries", "amount": 100,
                    "cadence": "monthly", "day": 15}],
        scheduled_transfers=[{"name": "Pay", "to": "Groceries", "amount": 100,
                              "cadence": "monthly", "day": 15}])
    r = forecast.forecast(cfg, {"g": 50000}, as_of=date(2026, 3, 1), through=date(2026, 3, 31))
    g = _env(r, "Groceries")
    assert g["same_day_funding_dependent"] is False


def test_relies_on_projected_income_when_inflow_is_load_bearing():
    # Sufficient only because a scheduled inflow lands before the bill. If that
    # inflow already posted into the starting balance, crediting it again would
    # be optimistic — so the dependence is flagged, not silently trusted.
    cfg = _cfg([GROC],
        recurring=[{"name": "Bill", "envelope": "Groceries", "amount": 100,
                    "cadence": "monthly", "day": 20}],
        scheduled_transfers=[{"name": "Pay", "to": "Groceries", "amount": 200,
                              "cadence": "monthly", "day": 10}])
    r = forecast.forecast(cfg, {"g": 0}, as_of=date(2026, 3, 1), through=date(2026, 3, 31))
    g = _env(r, "Groceries")
    assert g["verdict"] == "sufficient"
    assert g["relies_on_projected_income"] is True
    # Distinct from same-day dependence: the inflow and bill are on different days.
    assert g["same_day_funding_dependent"] is False


def test_no_projected_income_flag_when_balance_alone_covers_bills():
    cfg = _cfg([GROC],
        recurring=[{"name": "Bill", "envelope": "Groceries", "amount": 100,
                    "cadence": "monthly", "day": 20}],
        scheduled_transfers=[{"name": "Pay", "to": "Groceries", "amount": 200,
                              "cadence": "monthly", "day": 10}])
    r = forecast.forecast(cfg, {"g": 50000}, as_of=date(2026, 3, 1), through=date(2026, 3, 31))
    g = _env(r, "Groceries")
    assert g["verdict"] == "sufficient"
    assert g["relies_on_projected_income"] is False


def test_no_projected_income_flag_when_already_at_risk():
    cfg = _cfg([GROC],
        recurring=[{"name": "Bill", "envelope": "Groceries", "amount": 100,
                    "cadence": "monthly", "day": 20}])
    r = forecast.forecast(cfg, {"g": 0}, as_of=date(2026, 3, 1), through=date(2026, 3, 31))
    g = _env(r, "Groceries")
    assert g["verdict"] == "at_risk"
    assert g["relies_on_projected_income"] is False


def test_already_underwater_is_at_risk_as_of_today():
    cfg = _cfg([GROC])
    r = forecast.forecast(cfg, {"g": -500}, as_of=date(2026, 3, 1), through=date(2026, 3, 31))
    g = _env(r, "Groceries")
    assert g["verdict"] == "at_risk"
    assert g["at_risk_date"] == "2026-03-01"
    assert g["shortfall"] == 5.00


def test_no_scheduled_activity_is_sufficient():
    cfg = _cfg([GROC])
    r = forecast.forecast(cfg, {"g": 10000}, as_of=date(2026, 3, 1), through=date(2026, 3, 31))
    g = _env(r, "Groceries")
    assert g["verdict"] == "sufficient"
    assert g["projected_min_balance"] == 100.00
    assert g["n_inflows"] == 0 and g["n_outflows"] == 0


# --- balance_unknown ----------------------------------------------------------


def test_balance_unknown_when_account_absent():
    cfg = _cfg([GROC])
    r = forecast.forecast(cfg, {}, as_of=date(2026, 3, 1), through=date(2026, 3, 31))
    g = _env(r, "Groceries")
    assert g["verdict"] == "balance_unknown"
    assert g["current_balance"] is None
    assert g["projected_min_balance"] is None
    assert g["relies_on_projected_income"] is False
    assert r["summary"]["balance_unknown"] == 1


def test_partially_known_envelope_is_unknown_not_summed():
    # Two accounts, only one has a balance. The envelope must NOT silently treat
    # the missing one as zero.
    cfg = _cfg([{"name": "Groceries", "accounts": ["g", "g2"]}])
    r = forecast.forecast(cfg, {"g": 10000}, as_of=date(2026, 3, 1), through=date(2026, 3, 31))
    g = _env(r, "Groceries")
    assert g["verdict"] == "balance_unknown"
    assert g["current_balance"] is None


# --- window / occurrence semantics --------------------------------------------


def test_through_before_as_of_raises():
    cfg = _cfg([GROC])
    with pytest.raises(ValueError, match="window is empty"):
        forecast.forecast(cfg, {"g": 100}, as_of=date(2026, 3, 10), through=date(2026, 3, 1))


def test_occurrence_count_is_stable_over_a_fixed_window():
    cfg = _cfg([GROC], recurring=[
        {"name": "Bill", "envelope": "Groceries", "amount": 100,
         "cadence": "monthly", "day": 10},
    ])
    r = forecast.forecast(cfg, {"g": 100000}, as_of=date(2026, 3, 1), through=date(2026, 4, 30))
    g = _env(r, "Groceries")
    assert g["n_outflows"] == 2
    assert g["total_out"] == 200.00


def test_window_echoed_in_output():
    cfg = _cfg([GROC])
    r = forecast.forecast(cfg, {"g": 100}, as_of=date(2026, 3, 1), through=date(2026, 4, 30))
    assert r["as_of"] == "2026-03-01"
    assert r["through"] == "2026-04-30"


# --- account_balances_cents ---------------------------------------------------


def test_account_balances_parses_and_omits_unknown():
    accounts = [
        {"account_id": "g", "balance": "300.00"},
        {"account_id": "h", "balance": "-12.50"},
        {"account_id": "x", "balance": None},
        {"account_id": "y", "balance": ""},
        {"account_id": None, "balance": "5.00"},
    ]
    out = forecast.account_balances_cents(accounts)
    assert out == {"g": 30000, "h": -1250}  # x/y unparseable, None id dropped


# --- end-to-end through forecast_report ---------------------------------------


def test_forecast_report_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_MCP_HOME", str(tmp_path))
    conn = archive.connect()
    try:
        categories.seed_default_rules(conn)
        archive.upsert(conn, {
            "accounts": [
                {"account_id": "g", "account_name": "Groceries", "balance": "250.00"},
            ],
            # A transaction is needed so the archive view reads accounts from the
            # archive (not the empty JSON cache fallback).
            "transactions": [
                {"id": "t1", "account_id": "g", "amount": "-10.00",
                 "posted": "2026-03-02T00:00:00", "description": "WALMART"},
            ],
        })
    finally:
        conn.close()
    cfg = _cfg([GROC], recurring=[
        {"name": "Rent", "envelope": "Groceries", "amount": 300,
         "cadence": "monthly", "day": 15},
    ])
    r = forecast.forecast_report(cfg, as_of=date(2026, 3, 1), through=date(2026, 3, 31))
    g = _env(r, "Groceries")
    assert g["current_balance"] == 250.00
    assert g["verdict"] == "at_risk"  # 250 balance vs a 300 bill
    assert g["at_risk_date"] == "2026-03-15"
    assert g["shortfall"] == 50.00
