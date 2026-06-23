"""Tests for budget config parsing and validation."""

import json
from datetime import date

import pytest

from finance_mcp.budget_config import (
    BudgetConfigError,
    Envelope,
    load_config,
    parse_config,
)


def _cfg(**overrides):
    base = {
        "version": 1,
        "envelopes": [
            {"name": "Groceries", "accounts": ["schwab:294"], "monthly_target": 750.00},
            {"name": "Restaurants", "accounts": ["schwab:377"], "monthly_target": 225},
        ],
    }
    base.update(overrides)
    return base


def test_parses_valid_config():
    cfg = parse_config(_cfg())
    assert cfg.version == 1
    assert len(cfg.envelopes) == 2
    g = cfg.envelopes[0]
    assert g.name == "Groceries"
    assert g.accounts == ("schwab:294",)
    assert g.monthly_target_cents == 75000


def test_integer_target_is_dollars_not_cents():
    cfg = parse_config(_cfg())
    assert cfg.envelopes[1].monthly_target_cents == 22500


def test_account_index_maps_every_account():
    cfg = parse_config(
        _cfg(
            envelopes=[
                {"name": "Groceries", "accounts": ["a", "b"], "monthly_target": 10},
            ]
        )
    )
    idx = cfg.account_index()
    assert idx["a"].name == "Groceries"
    assert idx["b"].name == "Groceries"


def test_version_defaults_when_absent():
    data = _cfg()
    del data["version"]
    assert parse_config(data).version == 1


def test_rejects_non_object():
    with pytest.raises(BudgetConfigError, match="must be a JSON object"):
        parse_config([1, 2, 3])


def test_rejects_future_version():
    with pytest.raises(BudgetConfigError, match="newer than this tool"):
        parse_config(_cfg(version=999))


def test_rejects_missing_envelopes():
    with pytest.raises(BudgetConfigError, match="'envelopes' list"):
        parse_config({"version": 1})


def test_allows_empty_envelopes_list():
    cfg = parse_config({"version": 1, "envelopes": []})
    assert cfg.envelopes == ()


def test_rejects_blank_name():
    with pytest.raises(BudgetConfigError, match="name must be a non-empty"):
        parse_config(_cfg(envelopes=[{"name": "  ", "accounts": ["a"]}]))


def test_rejects_duplicate_names_case_insensitive():
    with pytest.raises(BudgetConfigError, match="duplicate envelope name"):
        parse_config(
            _cfg(
                envelopes=[
                    {"name": "Food", "accounts": ["a"]},
                    {"name": "food", "accounts": ["b"]},
                ]
            )
        )


def test_rejects_empty_accounts():
    with pytest.raises(BudgetConfigError, match="non-empty list"):
        parse_config(_cfg(envelopes=[{"name": "X", "accounts": []}]))


def test_rejects_blank_account_id():
    with pytest.raises(BudgetConfigError, match="non-empty string"):
        parse_config(_cfg(envelopes=[{"name": "X", "accounts": [""]}]))


def test_rejects_account_claimed_twice():
    with pytest.raises(BudgetConfigError, match="claimed by two envelopes"):
        parse_config(
            _cfg(
                envelopes=[
                    {"name": "A", "accounts": ["shared"]},
                    {"name": "B", "accounts": ["shared"]},
                ]
            )
        )


def test_rejects_negative_target():
    with pytest.raises(BudgetConfigError, match="must not be negative"):
        parse_config(_cfg(envelopes=[{"name": "X", "accounts": ["a"], "monthly_target": -5}]))


def test_rejects_non_whole_cent_target():
    with pytest.raises(BudgetConfigError, match="whole number of cents"):
        parse_config(
            _cfg(envelopes=[{"name": "X", "accounts": ["a"], "monthly_target": 10.005}])
        )


def test_rejects_boolean_target():
    with pytest.raises(BudgetConfigError, match="must be a number"):
        parse_config(
            _cfg(envelopes=[{"name": "X", "accounts": ["a"], "monthly_target": True}])
        )


def test_target_absent_is_none():
    cfg = parse_config(_cfg(envelopes=[{"name": "Hub", "accounts": ["a"]}]))
    assert cfg.envelopes[0].monthly_target_cents is None


def test_target_null_is_none():
    cfg = parse_config(
        _cfg(envelopes=[{"name": "Hub", "accounts": ["a"], "monthly_target": None}])
    )
    assert cfg.envelopes[0].monthly_target_cents is None


def test_role_optional_and_trimmed():
    cfg = parse_config(
        _cfg(envelopes=[{"name": "Hub", "accounts": ["a"], "role": " hub "}])
    )
    assert cfg.envelopes[0].role == "hub"


def test_rejects_non_string_role():
    with pytest.raises(BudgetConfigError, match="role must be a string"):
        parse_config(_cfg(envelopes=[{"name": "X", "accounts": ["a"], "role": 5}]))


def test_unknown_keys_ignored_for_forward_compat():
    cfg = parse_config(
        _cfg(
            future_section=[{"merchant": "Netflix"}],
            envelopes=[{"name": "X", "accounts": ["a"]}],
        )
    )
    assert len(cfg.envelopes) == 1


def test_string_target_accepted():
    cfg = parse_config(
        _cfg(envelopes=[{"name": "X", "accounts": ["a"], "monthly_target": "12.50"}])
    )
    assert cfg.envelopes[0].monthly_target_cents == 1250


def test_load_config_missing_file(tmp_path):
    with pytest.raises(BudgetConfigError, match="not found"):
        load_config(tmp_path / "nope.json")


def test_load_config_bad_json(tmp_path):
    p = tmp_path / "budget.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(BudgetConfigError, match="not valid JSON"):
        load_config(p)


def test_load_config_roundtrip(tmp_path):
    p = tmp_path / "budget.json"
    p.write_text(json.dumps(_cfg()), encoding="utf-8")
    cfg = load_config(p)
    assert isinstance(cfg.envelopes[0], Envelope)
    assert cfg.envelopes[0].monthly_target_cents == 75000

# --- Recurring calendar: bills ------------------------------------------------


def test_parses_recurring_bills():
    cfg = parse_config(
        _cfg(
            recurring=[
                {"name": "Rent", "envelope": "Groceries", "amount": 1500,
                 "cadence": "monthly", "day": 1},
            ]
        )
    )
    assert len(cfg.recurring) == 1
    b = cfg.recurring[0]
    assert b.name == "Rent"
    assert b.envelope == "Groceries"
    assert b.amount_cents == 150000
    assert b.cadence == "monthly"
    assert b.day == 1


def test_recurring_defaults_to_empty():
    cfg = parse_config(_cfg())
    assert cfg.recurring == ()
    assert cfg.scheduled_transfers == ()


def test_bill_envelope_must_exist():
    with pytest.raises(BudgetConfigError, match="does not match any configured envelope"):
        parse_config(_cfg(recurring=[
            {"name": "X", "envelope": "Nope", "amount": 10, "cadence": "monthly", "day": 5}
        ]))


def test_bill_envelope_resolves_case_insensitively():
    cfg = parse_config(_cfg(recurring=[
        {"name": "X", "envelope": "groceries", "amount": 10, "cadence": "monthly", "day": 5}
    ]))
    # Resolved to the canonical stored name, not the reference's casing.
    assert cfg.recurring[0].envelope == "Groceries"


def test_bill_without_envelope_but_with_match_parses():
    # A standalone subscription needs no envelope: a match keyword pins it to its
    # merchant so the audit can still tell whether it posted.
    cfg = parse_config(_cfg(recurring=[
        {"name": "Netflix", "amount": 15.99, "cadence": "monthly",
         "day": 10, "match": "NETFLIX"}
    ]))
    assert cfg.recurring[0].envelope is None
    assert cfg.recurring[0].match == "NETFLIX"


def test_bill_with_neither_envelope_nor_match_is_rejected():
    with pytest.raises(BudgetConfigError, match="needs either an 'envelope'"):
        parse_config(_cfg(recurring=[
            {"name": "Mystery", "amount": 10, "cadence": "monthly", "day": 5}
        ]))


def test_bill_amount_must_be_positive():
    with pytest.raises(BudgetConfigError, match="greater than zero"):
        parse_config(_cfg(recurring=[
            {"name": "X", "envelope": "Groceries", "amount": 0, "cadence": "monthly", "day": 5}
        ]))


def test_bill_amount_subcent_rejected():
    with pytest.raises(BudgetConfigError, match="whole number of cents"):
        parse_config(_cfg(recurring=[
            {"name": "X", "envelope": "Groceries", "amount": 10.005, "cadence": "monthly", "day": 5}
        ]))


def test_bill_unsupported_cadence_rejected():
    with pytest.raises(BudgetConfigError, match="cadence must be one of"):
        parse_config(_cfg(recurring=[
            {"name": "X", "envelope": "Groceries", "amount": 10, "cadence": "weekly", "day": 5}
        ]))


def test_bill_day_out_of_range_rejected():
    with pytest.raises(BudgetConfigError, match="day must be between 1 and 31"):
        parse_config(_cfg(recurring=[
            {"name": "X", "envelope": "Groceries", "amount": 10, "cadence": "monthly", "day": 32}
        ]))


def test_bill_match_optional_and_parsed():
    cfg = parse_config(_cfg(recurring=[
        {"name": "Netflix", "envelope": "Groceries", "amount": 16, "cadence": "monthly",
         "day": 10, "match": "NETFLIX"}
    ]))
    assert cfg.recurring[0].match == "NETFLIX"
    # Absent match defaults to None (backward compatible).
    cfg2 = parse_config(_cfg(recurring=[
        {"name": "Rent", "envelope": "Groceries", "amount": 1500, "cadence": "monthly", "day": 1}
    ]))
    assert cfg2.recurring[0].match is None


def test_bill_match_digits_only_rejected():
    # A keyword with no letter normalizes to no tokens and would match nothing,
    # so it must be rejected at parse time rather than silently never matching.
    with pytest.raises(BudgetConfigError, match="must contain at least one letter"):
        parse_config(_cfg(recurring=[
            {"name": "Gas", "envelope": "Groceries", "amount": 40, "cadence": "monthly",
             "day": 5, "match": "76"}
        ]))


def test_bill_match_empty_string_rejected():
    with pytest.raises(BudgetConfigError, match="match must be a non-empty string"):
        parse_config(_cfg(recurring=[
            {"name": "X", "envelope": "Groceries", "amount": 10, "cadence": "monthly",
             "day": 5, "match": "   "}
        ]))


def test_bill_match_accepts_letters_that_casefold_to_ascii():
    # A keyword whose only letter is non-ASCII but case-folds to an ASCII letter
    # (e.g. U+0130 -> 'i', U+212A KELVIN SIGN -> 'k') still normalizes to a
    # matchable token, so it must be accepted, not rejected.
    cfg = parse_config(_cfg(recurring=[
        {"name": "X", "envelope": "Groceries", "amount": 10, "cadence": "monthly",
         "day": 5, "match": "\u212aelvin"}
    ]))
    assert cfg.recurring[0].match == "\u212aelvin"


def test_bill_match_non_ascii_non_letter_rejected():
    # CJK / pure-symbol keywords normalize to no token and must fail loud.
    with pytest.raises(BudgetConfigError, match="must contain at least one letter"):
        parse_config(_cfg(recurring=[
            {"name": "X", "envelope": "Groceries", "amount": 10, "cadence": "monthly",
             "day": 5, "match": "\u4e2d\u6587"}
        ]))


def test_bill_day_must_be_int_not_bool():
    with pytest.raises(BudgetConfigError, match="day must be an integer"):
        parse_config(_cfg(recurring=[
            {"name": "X", "envelope": "Groceries", "amount": 10, "cadence": "monthly", "day": True}
        ]))


def test_bill_name_required():
    with pytest.raises(BudgetConfigError, match="name must be a non-empty"):
        parse_config(_cfg(recurring=[
            {"envelope": "Groceries", "amount": 10, "cadence": "monthly", "day": 5}
        ]))


def test_recurring_must_be_list():
    with pytest.raises(BudgetConfigError, match="'recurring' must be a list"):
        parse_config(_cfg(recurring={"not": "a list"}))


# --- Recurring calendar: lifecycle (cancellation watch) -----------------------


def test_lifecycle_defaults_to_active():
    cfg = parse_config(_cfg(recurring=[
        {"name": "X", "envelope": "Groceries", "amount": 10, "cadence": "monthly", "day": 5}
    ]))
    b = cfg.recurring[0]
    assert b.lifecycle == "active"
    assert b.cancel_effective is None


def test_lifecycle_canceled_parses_with_effective_date():
    cfg = parse_config(_cfg(recurring=[
        {"name": "Sketch", "envelope": "Groceries", "amount": 10, "cadence": "monthly",
         "day": 5, "lifecycle": "canceled", "cancel_effective": "2026-04-01"}
    ]))
    b = cfg.recurring[0]
    assert b.lifecycle == "canceled"
    assert b.cancel_effective == date(2026, 4, 1)


def test_lifecycle_canceling_parses_with_effective_date():
    cfg = parse_config(_cfg(recurring=[
        {"name": "Replit", "envelope": "Groceries", "amount": 10, "cadence": "monthly",
         "day": 5, "lifecycle": "canceling", "cancel_effective": "2026-04-01"}
    ]))
    assert cfg.recurring[0].lifecycle == "canceling"


def test_lifecycle_normalizes_case():
    cfg = parse_config(_cfg(recurring=[
        {"name": "X", "envelope": "Groceries", "amount": 10, "cadence": "monthly",
         "day": 5, "lifecycle": "Canceled", "cancel_effective": "2026-04-01"}
    ]))
    assert cfg.recurring[0].lifecycle == "canceled"


def test_invalid_lifecycle_rejected():
    with pytest.raises(BudgetConfigError, match="lifecycle must be one of"):
        parse_config(_cfg(recurring=[
            {"name": "X", "envelope": "Groceries", "amount": 10, "cadence": "monthly",
             "day": 5, "lifecycle": "paused", "cancel_effective": "2026-04-01"}
        ]))


def test_canceled_requires_cancel_effective():
    with pytest.raises(BudgetConfigError, match="needs a 'cancel_effective'"):
        parse_config(_cfg(recurring=[
            {"name": "X", "envelope": "Groceries", "amount": 10, "cadence": "monthly",
             "day": 5, "lifecycle": "canceled"}
        ]))


def test_active_must_not_set_cancel_effective():
    with pytest.raises(BudgetConfigError, match="only meaningful for"):
        parse_config(_cfg(recurring=[
            {"name": "X", "envelope": "Groceries", "amount": 10, "cadence": "monthly",
             "day": 5, "cancel_effective": "2026-04-01"}
        ]))


def test_cancel_effective_must_be_iso_date():
    with pytest.raises(BudgetConfigError, match="not a valid ISO date"):
        parse_config(_cfg(recurring=[
            {"name": "X", "envelope": "Groceries", "amount": 10, "cadence": "monthly",
             "day": 5, "lifecycle": "canceled", "cancel_effective": "04/01/2026"}
        ]))


def test_variable_defaults_to_false():
    cfg = parse_config(_cfg(recurring=[
        {"name": "X", "envelope": "Groceries", "amount": 10, "cadence": "monthly", "day": 5}
    ]))
    assert cfg.recurring[0].variable is False


def test_variable_true_parses():
    cfg = parse_config(_cfg(recurring=[
        {"name": "X", "envelope": "Groceries", "amount": 10, "cadence": "monthly",
         "day": 5, "match": "acme", "variable": True}
    ]))
    assert cfg.recurring[0].variable is True


def test_variable_non_bool_rejected():
    with pytest.raises(BudgetConfigError, match="variable must be true or false"):
        parse_config(_cfg(recurring=[
            {"name": "X", "envelope": "Groceries", "amount": 10, "cadence": "monthly",
             "day": 5, "variable": "yes"}
        ]))


def test_variable_without_match_rejected():
    with pytest.raises(BudgetConfigError, match="variable-amount bill must have a 'match'"):
        parse_config(_cfg(recurring=[
            {"name": "X", "envelope": "Groceries", "amount": 10, "cadence": "monthly",
             "day": 5, "variable": True}
        ]))


def test_variable_with_match_and_no_envelope_ok():
    cfg = parse_config(_cfg(recurring=[
        {"name": "X", "amount": 10, "cadence": "monthly", "day": 5,
         "match": "acme", "variable": True}
    ]))
    assert cfg.recurring[0].variable is True
    assert cfg.recurring[0].envelope is None


def test_tolerance_pct_defaults_to_zero():
    assert parse_config(_cfg()).recurring_amount_tolerance_pct == 0.0


def test_tolerance_pct_parses_fraction():
    cfg = parse_config(_cfg(recurring_amount_tolerance_pct=0.1))
    assert cfg.recurring_amount_tolerance_pct == 0.1


def test_tolerance_pct_out_of_range_rejected():
    with pytest.raises(BudgetConfigError, match="between 0 and 1"):
        parse_config(_cfg(recurring_amount_tolerance_pct=10))


def test_tolerance_pct_bool_rejected():
    with pytest.raises(BudgetConfigError, match="must be a number"):
        parse_config(_cfg(recurring_amount_tolerance_pct=True))


def test_cancel_effective_must_be_string():
    with pytest.raises(BudgetConfigError, match="must be an ISO date string"):
        parse_config(_cfg(recurring=[
            {"name": "X", "envelope": "Groceries", "amount": 10, "cadence": "monthly",
             "day": 5, "lifecycle": "canceled", "cancel_effective": 20260401}
        ]))


# --- Recurring calendar: scheduled transfers ----------------------------------


def test_parses_external_inflow():
    cfg = parse_config(_cfg(scheduled_transfers=[
        {"name": "Paycheck", "to": "Groceries", "amount": 500, "cadence": "monthly", "day": 15}
    ]))
    t = cfg.scheduled_transfers[0]
    assert t.to_envelope == "Groceries"
    assert t.from_envelope is None
    assert t.amount_cents == 50000
    assert t.day == 15


def test_parses_internal_transfer_with_from():
    cfg = parse_config(_cfg(scheduled_transfers=[
        {"name": "Fanout", "from": "Restaurants", "to": "Groceries",
         "amount": 200, "cadence": "monthly", "day": 1}
    ]))
    t = cfg.scheduled_transfers[0]
    assert t.from_envelope == "Restaurants"
    assert t.to_envelope == "Groceries"


def test_transfer_to_must_exist():
    with pytest.raises(BudgetConfigError, match="does not match any configured envelope"):
        parse_config(_cfg(scheduled_transfers=[
            {"name": "X", "to": "Nope", "amount": 10, "cadence": "monthly", "day": 5}
        ]))


def test_transfer_from_must_exist():
    with pytest.raises(BudgetConfigError, match="does not match any configured envelope"):
        parse_config(_cfg(scheduled_transfers=[
            {"name": "X", "from": "Nope", "to": "Groceries", "amount": 10,
             "cadence": "monthly", "day": 5}
        ]))


def test_transfer_from_and_to_must_differ():
    with pytest.raises(BudgetConfigError, match="must move between two different envelopes"):
        parse_config(_cfg(scheduled_transfers=[
            {"name": "X", "from": "Groceries", "to": "Groceries", "amount": 10,
             "cadence": "monthly", "day": 5}
        ]))


def test_scheduled_transfers_must_be_list():
    with pytest.raises(BudgetConfigError, match="'scheduled_transfers' must be a list"):
        parse_config(_cfg(scheduled_transfers="nope"))


# --- debt accounts ------------------------------------------------------------

def test_debt_accounts_default_empty():
    cfg = parse_config(_cfg())
    assert cfg.debt_accounts == ()


def test_debt_account_full_fields():
    cfg = parse_config(_cfg(debt_accounts=[
        {"account_id": "ACT-1", "label": "2nd Mortgage",
         "expected_amount": 752.24, "due_day": 1},
    ]))
    assert len(cfg.debt_accounts) == 1
    d = cfg.debt_accounts[0]
    assert d.account_id == "ACT-1"
    assert d.label == "2nd Mortgage"
    assert d.expected_amount_cents == 75224
    assert d.due_day == 1


def test_debt_account_optional_amount_and_day():
    cfg = parse_config(_cfg(debt_accounts=[
        {"account_id": "ACT-1", "label": "1st Mortgage"},
    ]))
    d = cfg.debt_accounts[0]
    assert d.expected_amount_cents is None
    assert d.due_day is None


def test_debt_account_requires_account_id():
    with pytest.raises(BudgetConfigError, match="account_id must be a non-empty string"):
        parse_config(_cfg(debt_accounts=[{"label": "X"}]))


def test_debt_account_requires_label():
    with pytest.raises(BudgetConfigError, match="label must be a non-empty string"):
        parse_config(_cfg(debt_accounts=[{"account_id": "ACT-1"}]))


def test_debt_account_rejects_duplicate_account():
    with pytest.raises(BudgetConfigError, match="listed twice"):
        parse_config(_cfg(debt_accounts=[
            {"account_id": "ACT-1", "label": "A"},
            {"account_id": "ACT-1", "label": "B"},
        ]))


def test_debt_account_rejects_bad_due_day():
    with pytest.raises(BudgetConfigError, match="day must be between 1 and 31"):
        parse_config(_cfg(debt_accounts=[
            {"account_id": "ACT-1", "label": "A", "due_day": 40},
        ]))


def test_debt_account_rejects_negative_amount():
    with pytest.raises(BudgetConfigError, match="expected_amount must not be negative"):
        parse_config(_cfg(debt_accounts=[
            {"account_id": "ACT-1", "label": "A", "expected_amount": -5},
        ]))


def test_debt_accounts_must_be_list():
    with pytest.raises(BudgetConfigError, match="'debt_accounts' must be a list"):
        parse_config(_cfg(debt_accounts="nope"))


def test_debt_account_payment_source_default_none():
    cfg = parse_config(_cfg(debt_accounts=[
        {"account_id": "ACT-1", "label": "1st Mortgage"},
    ]))
    assert cfg.debt_accounts[0].payment_source is None


def test_debt_account_payment_source_parsed():
    cfg = parse_config(_cfg(debt_accounts=[
        {"account_id": "ACT-1", "label": "1st Mortgage", "due_day": 1,
         "payment_source": {
             "account_id": "ACT-checking",
             "description_contains": ["CYPRUS MTG PMT", "MTG PMT LOAN"],
         }},
    ]))
    ps = cfg.debt_accounts[0].payment_source
    assert ps is not None
    assert ps.account_id == "ACT-checking"
    assert ps.description_contains == ("CYPRUS MTG PMT", "MTG PMT LOAN")


def test_debt_account_payment_source_account_id_optional():
    cfg = parse_config(_cfg(debt_accounts=[
        {"account_id": "ACT-1", "label": "L",
         "payment_source": {"description_contains": ["MTG PMT"]}},
    ]))
    ps = cfg.debt_accounts[0].payment_source
    assert ps.account_id is None
    assert ps.description_contains == ("MTG PMT",)


def test_payment_source_must_be_object():
    with pytest.raises(BudgetConfigError, match="payment_source must be an object"):
        parse_config(_cfg(debt_accounts=[
            {"account_id": "ACT-1", "label": "L", "payment_source": "nope"},
        ]))


def test_payment_source_requires_nonempty_patterns():
    with pytest.raises(BudgetConfigError, match="description_contains must be a non-empty list"):
        parse_config(_cfg(debt_accounts=[
            {"account_id": "ACT-1", "label": "L",
             "payment_source": {"description_contains": []}},
        ]))


def test_payment_source_rejects_blank_pattern():
    with pytest.raises(BudgetConfigError, match="description_contains entries must be non-empty"):
        parse_config(_cfg(debt_accounts=[
            {"account_id": "ACT-1", "label": "L",
             "payment_source": {"description_contains": ["ok", "  "]}},
        ]))


def test_payment_source_rejects_blank_account_id():
    with pytest.raises(BudgetConfigError, match="payment_source.account_id must be a non-empty string"):
        parse_config(_cfg(debt_accounts=[
            {"account_id": "ACT-1", "label": "L",
             "payment_source": {"description_contains": ["MTG"], "account_id": ""}},
        ]))
