"""Tests for budget config parsing and validation."""

import json

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
        _cfg(recurring=[{"merchant": "Netflix"}], envelopes=[{"name": "X", "accounts": ["a"]}])
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
